# WhatsApp (Evolution API)

Integração do canal. Fluxo geral do pipeline: [`ARCHITECTURE.md §4`](ARCHITECTURE.md).
Tabela `InboundMessage`: [`DATABASE.md`](DATABASE.md).

## 1. Responsabilidades do app `whatsapp`

| Módulo | Papel |
|---|---|
| `conf.py` | Lê `EVOLUTION_*`; falha com `ImproperlyConfigured` quando a operação precisa de um valor ausente |
| `parsers.py` | Único lugar que conhece o JSON da Evolution → `NormalizedMessage` |
| `services.py` | Reserva idempotente, filtros, identificação do remetente → `READY`/`IGNORED` |
| `pipeline.py` | `handle_ready_message(record)`: IA → validação → efeito atômico → resposta |
| `actions.py` | Contexto da IA, validação da proposta (`plan`) e execução (`execute`) no domínio |
| `messages.py` | Todos os textos enviados ao usuário (locais, nunca gerados pela IA) |
| `context.py` | Contexto curto por usuário (`ConversationContext`, TTL) para follow-ups |
| `views.py` | Webhook fino: autentica → parseia → service → status HTTP |
| `client.py` | HTTP para a Evolution (stdlib `urllib`), timeout, erros tipados |
| `admin.py` | `InboundMessage` somente leitura (diagnóstico, não é fila) |
| `management/commands/` | `evolution_check`, `evolution_set_webhook`, `evolution_send_test` |

O app não contém regra financeira; chama services/selectors dos apps de domínio.
O pipeline futuro consome `InboundMessage` (`READY`, com `user` e `text`), nunca o JSON bruto.

## 2. Contrato Evolution utilizado (v2.3.7)

Versão validada ao vivo: **Evolution API 2.3.7** (`evoapicloud/evolution-api:v2.3.7`),
integração `WHATSAPP-BAILEYS`. Contrato conferido no código-fonte dessa versão. Ao atualizar a
Evolution, revalidar esta seção e as fixtures de `apps/whatsapp/tests/payloads.py`.

Autenticação de saída: header `apikey: <EVOLUTION_API_KEY>`.

| Operação | Chamada |
|---|---|
| Versão/liveness | `GET /` → `{"version": "2.3.7", ...}` |
| Estado | `GET /instance/connectionState/{instance}` → `{"instance": {"state": "open"}}` |
| Enviar texto | `POST /message/sendText/{instance}` `{"number": "5531999998888", "text": "..."}` → `201`, `{"key": {"id": ...}, "status": "PENDING"}` |
| Webhook (upsert) | `POST /webhook/set/{instance}` `{"webhook": {"enabled", "url", "headers", "byEvents": false, "base64": false, "events": ["MESSAGES_UPSERT"]}}` |
| Consultar webhook | `GET /webhook/find/{instance}` (contém os headers — **não imprimir**) |
| Criar instância (manual, uma vez) | `POST /instance/create` `{"instanceName", "integration": "WHATSAPP-BAILEYS", "qrcode": true}` |

Webhook recebido (`messages.upsert`), campos usados:

```jsonc
{
  "event": "messages.upsert",          // normalizado para MESSAGES_UPSERT
  "instance": "<instance>",
  "data": {
    "key": {"remoteJid": "5531...@s.whatsapp.net", "fromMe": false, "id": "3EB0..."},
    "messageType": "conversation",     // texto (extendedText já vem convertido), audioMessage, imageMessage, ...
    "message": {"conversation": "10 reais de gasolina"},
    "messageTimestamp": 1791331200     // segundos (UTC)
  },
  "apikey": "<token da instância>"     // NUNCA persistido nem logado
}
```

- `@lid`: a v2.3.7 substitui `remoteJid` por `remoteJidAlt` quando conhecido. Se ainda vier
  `@lid` sem alternativa, o remetente fica não resolvido (`SENDER_UNRESOLVED`).
- Reentrega: a Evolution **não** repete respostas `400, 401, 403, 404, 422`; demais falhas são
  repetidas (até 10 tentativas, backoff exponencial).
- `base64: false` evita mídia embutida no webhook.
- Mensagens enviadas pela API (`sendText`) geram o evento `send.message`, não `messages.upsert`;
  `fromMe` chega no webhook quando alguém escreve pelo próprio aparelho do bot — e é ignorado.

Validado ao vivo em 2026-10-06 (instância `finance-whatsapp-dev`): conexão/versão, configuração
do webhook com header secreto, `401` sem segredo, evento não tratado ignorado, envio via
`evolution_send_test` e o pipeline completo com OpenAI ("10 reais de gasolina", "recebi 25
reais", "quanto gastei de transporte esse mês?").

## 3. Webhook

`POST /api/v1/webhooks/evolution/` — Django puro, `csrf_exempt` só nesta view (CSRF continua
ativo no resto da aplicação).

**Autenticação:** header `X-Webhook-Secret` com o valor de `EVOLUTION_WEBHOOK_SECRET`,
configurado em `webhook.headers` pelo comando `evolution_set_webhook`. Comparação com
`hmac.compare_digest`, antes de qualquer parse. O segredo nunca vai na URL nem em logs.

Política HTTP:

| Situação | Status | Efeito |
|---|---|---|
| Segredo ausente/inválido | `401` | nada processado |
| JSON inválido, corpo grande demais ou payload malformado | `400` | nada processado (não há retry) |
| `instance` ≠ `EVOLUTION_INSTANCE_NAME` | `403` | nada processado |
| Evento diferente de `MESSAGES_UPSERT` | `200` `ignored` | não registrado |
| Mensagem aceita / ignorada | `200` `ready` / `ignored` | registrada |
| Duplicata | `200` `duplicate` | nada reexecutado |
| Banco indisponível antes da reserva | `503` | Evolution repete (seguro: nada foi reservado) |
| Erro no pipeline após a reserva | `200`, status `FAILED` | retry seria no-op; reprocessamento é manual |
| `EVOLUTION_WEBHOOK_SECRET` não configurado | `500` | falha explícita de configuração |

## 4. Mensagem normalizada e filtros

`parsers.NormalizedMessage` (dataclass imutável; `repr` sem texto/telefone): `provider`,
`instance`, `provider_message_id`, `remote_jid`, `sender_phone` (E.164 via `common.phone`
ou `None`), `message_type` (`TEXT`/`AUDIO`/`IMAGE`/`UNSUPPORTED`), `text`, `timestamp`
(aware, UTC), `from_me`, `is_group`, `is_broadcast`, `metadata` (`provider_message_type`).

Classificação em `services.receive_message`, nesta ordem (resultado → `ignore_reason`):

1. `fromMe` → `FROM_ME` (evita loop com as próprias respostas).
2. Grupo (`@g.us`) → `GROUP`; broadcast/status → `BROADCAST`.
3. Telefone não resolvido (ex.: `@lid` sem alternativa) → `SENDER_UNRESOLVED`.
4. Número não cadastrado → `UNKNOWN_SENDER`. **Usuário não é criado e não há resposta**
   (evita spam e enumeração de números). Cadastro continua no Django Admin.
5. Usuário inativo → `INACTIVE_USER`.
6. Áudio/imagem de usuário ativo → fica `RECEIVED` e segue para `audio.py` / `image.py` (§7).
   Os filtros 1–5 rodam antes: nada é baixado para mensagem ignorada.
7. Outros tipos → `UNSUPPORTED_TYPE`. Texto vazio → `EMPTY_TEXT`; > 4096 caracteres → `TEXT_TOO_LONG`.
8. Texto de usuário ativo → `READY` (com `user` e `text`).

## 5. Idempotência e estados

Ordem: autenticar → parsear → **reservar** → classificar → pipeline.

- Reserva = `INSERT` em `InboundMessage` com `UNIQUE (provider, instance, provider_message_id)`,
  dentro de `transaction.atomic()`. Requisições concorrentes: o banco aceita uma; a outra recebe
  `IntegrityError` e é tratada como duplicata. Não há `if exists` como proteção.
- A chave inclui `provider` e `instance` porque o id do WhatsApp só é único dentro da conta.
- Duplicatas retornam antes de qualquer efeito (pipeline, envio de mensagem).
- Segunda camada: `Transaction.idempotency_key = "whatsapp:<instance>:<id>"` (`UNIQUE`).

Pipeline (`pipeline.handle_ready_message`), sem transação aberta durante HTTP:

1. Mensagem não `READY` → nada é feito (nunca vai para a IA).
2. Contexto + **uma** chamada OpenAI. Falha → `FAILED` (`AI_<código>`), resposta genérica.
3. `actions.plan`: validação Django da proposta (leitura apenas).
4. `transaction.atomic()`: `select_for_update` da mensagem; se não estiver mais `READY`, para;
   executa (`create_transaction` / `update_transaction` / `undo_last_change` / selectors),
   grava o contexto da consulta e `PROCESSED` + `intent` + `transaction` (afetada).
   Efeito e status commitam juntos; qualquer exceção desfaz ambos → `FAILED` (`PIPELINE_ERROR`).
5. Resposta enviada **depois** do commit. Falha no envio → continua `PROCESSED`, `error_code =
   REPLY_SEND_FAILED`; nada é reexecutado (reentrega = duplicata; status ≠ READY).

| Status | Significado |
|---|---|
| `RECEIVED` | Reservada; classificação não terminou (se persistir, houve falha antes de classificar) |
| `IGNORED` | Não processada de propósito; ver `ignore_reason` |
| `READY` | Usuário conhecido + texto; aguardando/no pipeline de interpretação |
| `PROCESSED` | Tratada e respondida: efeito ou consulta concluídos. `error_code` preenchido = respondida com rejeição sem efeito (`MISSING_AMOUNT`, `REJECTED_AMOUNT`, `NOT_UNDERSTOOD`, ...) ou `REPLY_SEND_FAILED` |
| `FAILED` | Falha técnica sem efeito financeiro (`AI_*`, `PIPELINE_ERROR`); não é reprocessada automaticamente |

Dados persistidos: campos técnicos + `sender_phone` + `text` **somente** para `READY`.
Payload bruto **não** é armazenado (contém `apikey` da instância e dados pessoais).
Retenção de `InboundMessage` deve ser definida antes da produção.

## 6. Envio e operação

- `EvolutionClient.send_text(phone, text)`: normaliza o destino por `common.phone`, valida
  texto (1–4096), usa `EVOLUTION_INSTANCE_NAME`, timeout `EVOLUTION_TIMEOUT_SECONDS`.
  Erros: `EvolutionTimeoutError`, `EvolutionConnectionError`, `EvolutionHTTPError(status_code)`,
  `EvolutionResponseError`. Sem retry próprio.
- Respostas ao usuário: mensagens curtas, pt-BR, `R$ 1.234,56`, `dd/mm/aaaa`; falha no envio
  é registrada e não desfaz a transação.
- Textos: `apps/whatsapp/messages.py`. Exemplos: "✅ Gasto registrado: R$ 10,00 em Transporte ·
  Combustível.", "✅ Entrada registrada: R$ 25,00 em Outras receitas.", "Você gastou R$ 10,00 com
  Transporte em outubro.", "Seu saldo em outubro é R$ 15,00 (entradas R$ 25,00; gastos R$ 10,00).",
  "✏️ Gasto atualizado: R$ 10,00 → R$ 15,00 em Transporte · Combustível.", "↩️ Alteração
  desfeita. O gasto voltou para R$ 10,00 em Transporte · Combustível.", "↩️ Registro de
  R$ 10,00 em Transporte · Combustível desfeito.", listas numeradas (até 10 linhas) para
  maiores gastos/por categoria/últimos lançamentos e "Você gastou R$ 850,00 em outubro e
  R$ 700,00 em setembro: R$ 150,00 a mais (+21,4%)."
- V1.2 (família, ADMIN): "A família gastou R$ 1.730,00 em outubro.", "Maria gastou R$ 420,00 com
  Alimentação em outubro.", "Gastos por pessoa em outubro:\n1. Alice — R$ 800,00 ...",
  "Maiores gastos da família ...: 1. Mercado (Maria) — R$ 420,00". Negações discretas em
  `DOMAIN.md §6`. Nunca telefone de membro em resposta.

Comandos (nunca imprimem API key nem segredo):

```bash
python manage.py evolution_check                       # versão, estado, webhook, header confere?
python manage.py evolution_set_webhook --url https://<host>/api/v1/webhooks/evolution/
python manage.py evolution_send_test --phone 31999998888 [--text "..."]
```

Desenvolvimento local:

- `EVOLUTION_API_BASE_URL` é a URL vista **pelo Django**: no container `web`, use
  `http://host.docker.internal:8080`; rodando no host, `http://localhost:8080`.
- A URL do webhook é a vista **pela Evolution**: com Evolution em Docker e Django no host,
  `http://host.docker.internal:8000/api/v1/webhooks/evolution/` (inclua `host.docker.internal`
  em `DJANGO_ALLOWED_HOSTS`). Com Evolution remota, use um túnel HTTPS (qualquer ferramenta);
  o túnel não faz parte da aplicação nem do container Django.
- Pareamento: após criar a instância, escanear o QR Code no Manager
  (`<EVOLUTION_API_BASE_URL>/manager`) ou via `GET /instance/connect/{instance}`.

## 7. Mídia (versões futuras)

### Áudio — V1.3 (implementado)

```
webhook (audioMessage de usuário ativo) → reserva (RECEIVED)
  → limites declarados (seconds/fileLength do payload; dica não confiável, evita download)
  → download: POST /chat/getBase64FromMediaMessage/{instance} {"message":{"key":{"id":<id>}},"convertToMp4":false}
  → formato pelos bytes (OGG/Opus, MP3, M4A/MP4, WAV, WEBM) — sem conversão, sem ffmpeg
  → 1 chamada de transcrição (OPENAI_TRANSCRIPTION_MODEL, language="pt")
  → UPDATE ... WHERE status=RECEIVED → READY (text = transcrição) → pipeline textual existente
```

- **Download (Evolution 2.3.7):** só o id da mensagem é enviado; a Evolution busca a mensagem no
  próprio banco (`DATABASE_SAVE_DATA_NEW_MESSAGE=true`, obrigatório) e baixa/decifra a mídia.
  Nenhuma URL, `mediaKey`, `directPath` ou `fileName` do webhook é repassado ou acessado (sem SSRF).
  Resposta lida com teto (`AUDIO_MAX_BYTES` em base64 + 64 KiB), `mediaType` precisa ser
  `audioMessage`, base64 validado (`validate=True`) e tamanho checado antes e depois de decodificar.
- **Aceito:** nota de voz (`ptt=true`) e arquivo de áudio (`audioMessage`). `documentMessage` com
  extensão/MIME de áudio continua `UNSUPPORTED`.
- **Limites:** `AUDIO_MAX_BYTES` (padrão 10 MiB; API aceita 25 MB) e `AUDIO_MAX_SECONDS`
  (padrão 300 s, pela duração declarada — não há biblioteca para medir). Texto transcrito > 4096
  caracteres é rejeitado como qualquer texto.
- **Persistência:** nenhum byte/base64 de áudio é gravado (nem em disco: tudo em memória, o SDK
  recebe `("audio.<ext>", bytes, content_type)` com nome e tipo definidos por nós). Só a
  transcrição vai para `InboundMessage.text`, como qualquer mensagem `READY`. Transações de áudio
  têm `source=AUDIO` (auditoria); as regras são as mesmas do texto.
- **Estados/códigos:** download falhou → `FAILED` `AUDIO_DOWNLOAD_FAILED` ("Não consegui acessar
  esse áudio..."); transcrição falhou → `FAILED` `TRANSCRIPTION_<código>` (resposta genérica);
  limite/forma → `PROCESSED` + `AUDIO_TOO_LARGE` / `AUDIO_TOO_LONG` / `AUDIO_UNSUPPORTED_FORMAT` /
  `EMPTY_TRANSCRIPT` / `EMPTY_AUDIO` / `TRANSCRIPT_TOO_LONG` (resposta curta, sem efeito). Nenhum
  desses casos chama a interpretação financeira.
- **Idempotência:** reentrega para na reserva (sem novo download/transcrição); a promoção para
  READY é condicional (`status=RECEIVED`); depois vale a idempotência do pipeline textual.
- **Respostas:** sempre texto (sem TTS).

### Imagem / comprovante — V1.4 (implementado)

```
webhook (imageMessage de usuário ativo) → reserva (RECEIVED)
  → tamanho declarado (dica) → download por id (mesmo endpoint do áudio, mediaType=imageMessage)
  → common.images.normalize_image: formato pelos bytes (JPEG/PNG/WEBP/GIF estático), Pillow,
    limite de pixels antes de decodificar, orientação EXIF aplicada, metadados (EXIF/GPS/texto)
    removidos na regravação, SHA-256
  → upload no bucket PRIVADO (receipts/AAAA/MM/<uuid>.<ext>, x-upsert=false) → ReceiptAttachment STORED
  → 1 chamada de visão (OPENAI_VISION_MODEL, input_image data URL, detail=high, schema estrito)
  → actions.plan_receipt (regras Django) → pipeline.commit_plan (create_transaction, source=IMAGE,
    vínculo do anexo e PROCESSED no mesmo atomic) → resposta local
```

- **Registro direto, sem etapa de confirmação**: só cria quando tudo é inequívoco (documento
  financeiro, direção, valor, BRL, data); o usuário pode desfazer com "desfazer". Casos incertos
  respondem pedindo o dado em texto (sem conversa multimodal).
- **Sem data impressa** → data de recebimento da mensagem no fuso de negócio; data ilegível/
  inválida → não registra; data futura → recusada pelo service.
- **Pix**: "enviado/pago" → EXPENSE; "recebido" → INCOME; direção não clara → "Esse comprovante é
  de um valor pago ou recebido? Envie em texto...". Moeda ≠ BRL → não converte.
- **Códigos**: `NOT_A_RECEIPT`, `AMOUNT_UNREADABLE`, `FOREIGN_CURRENCY`, `CURRENCY_UNCLEAR`,
  `DIRECTION_UNCLEAR`, `DATE_UNREADABLE`, `REJECTED_*`, `IMAGE_*` (PROCESSED, sem efeito);
  `IMAGE_DOWNLOAD_FAILED`, `STORAGE_<código>`, `VISION_<código>` (FAILED, sem efeito).
- **Ordem**: Storage antes da visão (evidência preservada). Falha de upload → não chama visão.
  Falha de visão após upload → anexo `FAILED`, arquivo mantido. Sem retry automático.
- **Idempotência**: reentrega para na reserva (sem novo download/upload/visão);
  `ReceiptAttachment.inbound_message` é one-to-one; a transação usa a mesma `idempotency_key`.
- **Resposta**: "✅ Gasto registrado: R$ 47,90 em Alimentação. Comprovante armazenado." — nunca
  URL do Storage; nenhuma imagem é enviada de volta.

Toda mídia é entrada não confiável: validar MIME/tamanho, nunca executar nem renderizar.
