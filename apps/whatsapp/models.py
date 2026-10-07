from django.conf import settings
from django.db import models


class MessageType(models.TextChoices):
    TEXT = "TEXT", "Texto"
    AUDIO = "AUDIO", "Áudio"
    IMAGE = "IMAGE", "Imagem"
    UNSUPPORTED = "UNSUPPORTED", "Não suportado"


class InboundMessage(models.Model):
    """Technical record of a message received from the provider.

    The unique (provider, instance, provider_message_id) row is the idempotency reservation:
    it is inserted before any processing, so a redelivered webhook never runs business logic
    twice. Status meanings are documented in docs/WHATSAPP.md.
    """

    class Provider(models.TextChoices):
        EVOLUTION = "EVOLUTION", "Evolution API"

    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "Recebida"  # reserved, classification not finished
        IGNORED = "IGNORED", "Ignorada"  # deliberately not processed (see ignore_reason)
        READY = "READY", "Pronta"  # known user + text, ready for the interpretation pipeline
        PROCESSED = "PROCESSED", "Processada"  # handled and answered (effect or reply committed)
        FAILED = "FAILED", "Falhou"  # technical failure, no financial effect; not retried

    class IgnoreReason(models.TextChoices):
        FROM_ME = "FROM_ME", "Enviada pela própria instância"
        GROUP = "GROUP", "Mensagem de grupo"
        BROADCAST = "BROADCAST", "Broadcast/status"
        SENDER_UNRESOLVED = "SENDER_UNRESOLVED", "Telefone do remetente não identificado"
        UNKNOWN_SENDER = "UNKNOWN_SENDER", "Número não cadastrado"
        INACTIVE_USER = "INACTIVE_USER", "Usuário inativo"
        MEDIA_NOT_SUPPORTED = "MEDIA_NOT_SUPPORTED", "Áudio/imagem ainda não suportados"
        UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE", "Tipo de mensagem não suportado"
        EMPTY_TEXT = "EMPTY_TEXT", "Texto vazio"
        TEXT_TOO_LONG = "TEXT_TOO_LONG", "Texto longo demais"

    provider = models.CharField(max_length=20, choices=Provider.choices, default=Provider.EVOLUTION)
    instance = models.CharField("instância", max_length=100)
    provider_message_id = models.CharField("id da mensagem no provider", max_length=128)
    message_type = models.CharField("tipo", max_length=12, choices=MessageType.choices)
    sender_phone = models.CharField("telefone (E.164)", max_length=16, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inbound_messages",
        verbose_name="usuário",
    )
    text = models.TextField(
        "texto",
        blank=True,
        help_text="Armazenado somente para mensagens READY de usuários conhecidos.",
    )
    provider_timestamp = models.DateTimeField("horário no provider", null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RECEIVED)
    ignore_reason = models.CharField(
        "motivo", max_length=20, choices=IgnoreReason.choices, blank=True
    )
    error_code = models.CharField("código de erro", max_length=50, blank=True)
    intent = models.CharField("intenção interpretada", max_length=30, blank=True)
    transaction = models.ForeignKey(
        "transactions.Transaction",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inbound_messages",
        verbose_name="transação criada",
    )
    received_at = models.DateTimeField("recebida em", auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    processed_at = models.DateTimeField("finalizada em", null=True, blank=True)

    class Meta:
        verbose_name = "mensagem recebida"
        verbose_name_plural = "mensagens recebidas"
        ordering = ["-received_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "instance", "provider_message_id"],
                name="inbound_message_provider_id_unique",
            ),
            models.CheckConstraint(
                condition=~models.Q(provider_message_id=""),
                name="inbound_message_provider_id_not_empty",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=["RECEIVED", "IGNORED", "READY", "PROCESSED", "FAILED"]
                ),
                name="inbound_message_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(message_type__in=["TEXT", "AUDIO", "IMAGE", "UNSUPPORTED"]),
                name="inbound_message_type_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "received_at"], name="inbound_message_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.instance}:{self.provider_message_id} ({self.status})"
