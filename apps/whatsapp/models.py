import uuid

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


class ConversationContext(models.Model):
    """Minimal per-user conversation state: the last *validated* query parameters.

    Used only to resolve follow-ups ("e mês passado?"). Holds ids/dates/kinds, never amounts,
    texts or other users' data, and expires after CONVERSATION_CONTEXT_TTL_MINUTES. It never
    grants access: queries always run for the message sender.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="conversation_context",
        verbose_name="usuário",
    )
    last_query = models.JSONField("última consulta")
    expires_at = models.DateTimeField("expira em")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "contexto de conversa"
        verbose_name_plural = "contextos de conversa"

    def __str__(self) -> str:
        return f"Contexto de {self.user_id} até {self.expires_at:%H:%M}"


class PendingAction(models.Model):
    """A multi-transaction mutation waiting for the sender's explicit confirmation ("sim").

    One row per user (one-to-one): a new request replaces it. It is tied to the message that
    asked (``inbound_message``): only the sender's very next message may answer it, so any
    message in between — even one that failed before doing anything — invalidates it.
    Holds the exact set of transaction ids presented, their count/total, the approved criteria
    and a ``fingerprint`` of every candidate's relevant fields (to detect any change, even one
    that keeps the total), and expires after a few minutes. Never grants access: on
    confirmation the domain service re-checks ownership of every id.
    """

    class Action(models.TextChoices):
        DELETE_MANY = "DELETE_MANY", "Excluir vários lançamentos"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pending_action",
        verbose_name="usuário",
    )
    inbound_message = models.ForeignKey(
        InboundMessage,
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name="mensagem do pedido",
    )
    action = models.CharField("ação", max_length=20, choices=Action.choices)
    transaction_ids = models.JSONField("transações")
    count = models.PositiveIntegerField("quantidade")
    total = models.DecimalField("total", max_digits=14, decimal_places=2)
    criteria = models.JSONField("critérios aprovados")
    fingerprint = models.CharField("impressão dos candidatos", max_length=64)
    origin = models.CharField("origem", max_length=200)
    expires_at = models.DateTimeField("expira em")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "ação pendente de confirmação"
        verbose_name_plural = "ações pendentes de confirmação"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(action__in=["DELETE_MANY"]),
                name="pending_action_action_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(count__gt=0), name="pending_action_count_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(fingerprint__regex=r"^[0-9a-f]{64}$"),
                name="pending_action_fingerprint_sha256",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.action} de {self.user_id} até {self.expires_at:%H:%M}"


class ReceiptAttachment(models.Model):
    """Technical metadata of a receipt/proof image received by WhatsApp.

    The image itself is processed ephemerally in memory and never retained (no bytes, no
    base64, no file, no URL, no OCR text). One row per inbound message (one-to-one), written
    only after the vision call: PROCESSED in the same atomic commit as the transaction (if any),
    or FAILED when vision/commit failed. ``sha256`` is the hash of the normalized bytes sent to
    the vision model, for technical audit only (never used for deduplication).

    ``storage_bucket``/``storage_path`` and status STORED are LEGACY: only rows created before
    images stopped being stored (V1.4 Storage era) have them; new rows always keep them NULL.
    """

    class Status(models.TextChoices):
        STORED = "STORED", "Armazenado (legado)"  # legacy: uploaded, extraction pending
        PROCESSED = "PROCESSED", "Processado"  # extraction answered (with or without a transaction)
        FAILED = "FAILED", "Falhou"  # vision or commit failed; no transaction

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inbound_message = models.OneToOneField(
        InboundMessage,
        on_delete=models.PROTECT,
        related_name="receipt",
        verbose_name="mensagem",
    )
    transaction = models.ForeignKey(
        "transactions.Transaction",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="receipts",
        verbose_name="transação",
    )
    status = models.CharField(max_length=10, choices=Status.choices)
    error_code = models.CharField("código de erro", max_length=50, blank=True)
    document_type = models.CharField("tipo de documento", max_length=20, blank=True)
    # Legacy (images are no longer stored): NULL for every new row. NULL (not "") on purpose:
    # "never stored" must be unambiguous and the path keeps its UNIQUE constraint.
    storage_bucket = models.CharField(  # noqa: DJ001
        "bucket (legado)", max_length=100, null=True, blank=True
    )
    storage_path = models.CharField(
        "caminho (legado)", max_length=255, unique=True, null=True, blank=True
    )
    content_type = models.CharField("tipo", max_length=30)
    byte_size = models.PositiveIntegerField("bytes")
    sha256 = models.CharField(max_length=64, db_index=True)
    width = models.PositiveIntegerField("largura")
    height = models.PositiveIntegerField("altura")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "comprovante"
        verbose_name_plural = "comprovantes"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=["STORED", "PROCESSED", "FAILED"]),
                name="receipt_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(content_type__in=["image/jpeg", "image/png", "image/webp"]),
                name="receipt_content_type_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(storage_path__isnull=True)
                | models.Q(storage_path__startswith="receipts/"),
                name="receipt_storage_path_opaque",
            ),
            models.CheckConstraint(
                condition=models.Q(byte_size__gt=0),
                name="receipt_byte_size_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"Comprovante {self.id} ({self.status})"
