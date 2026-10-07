# Banco de dados

PostgreSQL gerenciado pelo Supabase. Regras de negócio que motivam as constraints estão em
[`DOMAIN.md`](DOMAIN.md). Conexão e operação: [`DEPLOYMENT.md`](DEPLOYMENT.md).

## 1. Princípios

- Integridade é garantida **no banco** (constraints, FKs, unicidade), não só em código.
- Schema muda apenas por migrations Django versionadas. Nunca alterar tabelas pelo
  painel do Supabase.
- Dinheiro: `DecimalField(max_digits=14, decimal_places=2)` → `NUMERIC(14,2)`. Nunca `FloatField`.
- Datas financeiras: `DateField`. Timestamps: `DateTimeField` (`timestamptz`), `USE_TZ=True`.
- Enums como `TextChoices` armazenados em `varchar` + `CheckConstraint` quando crítico.
- FKs para dados financeiros usam `on_delete=PROTECT`. Nada de cascade apagando transações.
- Agregações via ORM (`aggregate`, `annotate`) — o banco calcula.
- Testes usam PostgreSQL real (nunca SQLite) em container local; ver `TESTING.md`.
- Configuração por `DB_*` (ver `.env.example`); sem configuração a aplicação falha com
  `ImproperlyConfigured` — não há fallback para SQLite.

## 2. Tabelas previstas

Nomes de campo indicativos; o agent pode ajustar nomenclatura mantendo semântica e constraints.

### `accounts.User`

| Campo | Tipo | Regras |
|---|---|---|
| (campos de `AbstractUser`) | | |
| `phone_number` | varchar(20), null | E.164; `UNIQUE` (nulos permitidos); validado em `clean`/service |

### `families.Family`

| Campo | Tipo | Regras |
|---|---|---|
| `id` | bigint | PK |
| `name` | varchar | obrigatório |
| `created_at`, `updated_at` | timestamptz | |

### `families.FamilyMember`

| Campo | Tipo | Regras |
|---|---|---|
| `family` | FK Family | `CASCADE` (membership não é dado financeiro) |
| `user` | OneToOne User | `UNIQUE` (um usuário, uma família — decisão inicial); `CASCADE` |
| `role` | varchar | `ADMIN` \| `MEMBER`; `CHECK`; unique parcial `(family) WHERE role='ADMIN'` |
| `created_at` | timestamptz | |

### `categories.Category`

| Campo | Tipo | Regras |
|---|---|---|
| `name` | varchar | `UNIQUE (lower(name))` |
| `slug` | varchar | `UNIQUE`; chave estável usada pelo seed; não mudar após uso (a IA recebe o `id`) |
| `kind` | varchar | `EXPENSE` \| `INCOME` \| `BOTH`; `CHECK` |
| `description` | text | opcional |
| `aliases` | `text[]` (ArrayField) | default vazio; normalizados (minúsculas, sem duplicatas) |
| `is_active` | bool | default true |
| `sort_order` | int | |
| `created_at`, `updated_at` | timestamptz | |
| `is_fallback` | bool | unique parcial `(kind) WHERE is_fallback`; `CHECK` fallback ativa e não `BOTH` |

### `categories.Subcategory`

| Campo | Tipo | Regras |
|---|---|---|
| `category` | FK Category | `PROTECT` |
| `name` | varchar | |
| `slug` | varchar | `UNIQUE (category, slug)`; `UNIQUE (category, lower(name))` |
| `description`, `aliases`, `is_active`, `sort_order`, timestamps | | como em `Category` |

### `transactions.Transaction`

| Campo | Tipo | Regras |
|---|---|---|
| `id` | UUID | PK |
| `user` | FK User | dono; `PROTECT`; índice |
| `family` | FK Family, null | família do dono na criação (derivada pelo service); `PROTECT` |
| `type` | varchar | `EXPENSE` \| `INCOME`; `CHECK` |
| `amount` | `NUMERIC(14,2)` | `CHECK (amount > 0)` |
| `category` | FK Category | `PROTECT` |
| `subcategory` | FK Subcategory, null | `PROTECT`; pertinência à categoria validada no service e em `Model.clean` (sem FK composta no Django) |
| `description` | varchar(255) | pode ser vazio |
| `transaction_date` | date | data financeira |
| `source` | varchar | `TEXT` \| `AUDIO` \| `IMAGE` \| `MANUAL` \| `ADMIN`; `CHECK` |
| `idempotency_key` | varchar, null | `UNIQUE` (nulos permitidos); `CHECK <> ''`. Ex.: `whatsapp:<instance>:<message_id>` |
| `created_at`, `updated_at` | timestamptz | |
| `deleted_at` | timestamptz, null | soft delete |

Índices: `(user, transaction_date)`, `(user, category, transaction_date)` e, desde a V1.5,
`transaction_family_date_idx` `(family, transaction_date)` (migration `transactions/0003`,
somente `CREATE INDEX`; aplicada no Supabase DEV em 2026-10-07, RLS inalterado em 19/19
tabelas, nenhuma policy criada/alterada). Considerar índice parcial `WHERE deleted_at IS NULL`
quando o volume justificar.

Querysets: o manager padrão não deve esconder registros excluídos de forma implícita no
Admin; selectors de domínio **sempre** filtram `deleted_at IS NULL` explicitamente ou via
queryset method nomeado (`.active()`).

> V1.2 não criou tabelas nem colunas: consultas familiares usam `Transaction.family` (já
> indexável via FK) e `FamilyMember`. Nenhuma alteração de RLS.
>
> V1.5 (análises) não criou tabelas, colunas nem snapshots de relatório: tudo é agregado sob
> demanda (`Sum`/`Count` condicionais, `TruncMonth` sobre `transaction_date`) — só o índice acima.

### `transactions.TransactionChange` (V1.1)

| Campo | Tipo | Regras |
|---|---|---|
| `transaction` | FK Transaction | `PROTECT` |
| `user` | FK User | autor; `PROTECT`; índice `(user, created_at)` |
| `action` | varchar | `CREATE` \| `UPDATE` \| `DELETE` \| `UNDO`; `CHECK` |
| `before`, `after` | jsonb, null | `amount` (string), `description`, `category_id`, `subcategory_id`, `transaction_date` |
| `reverts` | OneToOne self, null | só em `UNDO` (`CHECK`); unicidade = cada ação desfeita no máximo uma vez |
| `origin` | varchar | ex.: `whatsapp:<instância>:<id>` |
| `created_at` | timestamptz | |

### `whatsapp.ConversationContext` (V1.1)

| Campo | Tipo | Regras |
|---|---|---|
| `user` | OneToOne User | `CASCADE`; uma linha por usuário |
| `last_query` | jsonb | parâmetros validados da última consulta (ids, datas, tipo, limite) |
| `expires_at` | timestamptz | `now + CONVERSATION_CONTEXT_TTL_MINUTES` |
| `updated_at` | timestamptz | |

### `whatsapp.ReceiptAttachment` (V1.4)

| Campo | Tipo | Regras |
|---|---|---|
| `id` | UUID | PK |
| `inbound_message` | OneToOne InboundMessage | `PROTECT`; um anexo por mensagem (idempotência) |
| `transaction` | FK Transaction, null | `PROTECT`; preenchida quando a transação é criada |
| `status` | varchar | `STORED` \| `PROCESSED` \| `FAILED`; `CHECK` |
| `error_code`, `document_type` | varchar | códigos sanitizados / `RECEIPT`, `PAYMENT_PROOF`, ... |
| `storage_bucket`, `storage_path` | varchar | path `UNIQUE`, `CHECK` começa com `receipts/` (opaco, sem dados pessoais) |
| `content_type` | varchar | `image/jpeg` \| `image/png` \| `image/webp`; `CHECK` |
| `byte_size`, `width`, `height` | int | da imagem normalizada; `byte_size > 0` |
| `sha256` | varchar(64) | índice; diagnóstico (não bloqueia reenvio legítimo) |
| `created_at`, `updated_at` | timestamptz | |

Nunca: bytes, base64, URL (pública ou assinada), chave, payload bruto. RLS habilitado como nas
demais tabelas.

### Supabase Storage (V1.4)

- Bucket **privado** (`SUPABASE_STORAGE_BUCKET`), acessado só pelo backend com a
  `SUPABASE_SERVICE_ROLE_KEY` (que **ignora RLS**, por isso nunca sai do servidor: não vai para
  banco, logs, OpenAI ou clientes). Nenhuma policy pública, nenhuma URL pública.
- `python manage.py storage_check [--write-test]` confirma que o bucket existe e é privado (e
  testa upload/consulta/remoção com objeto sintético). Nunca altera políticas.
- `RETENTION_POLICY = PENDING_PRODUCT_DECISION`: sem expurgo automático; definir antes de produção.

### `whatsapp.InboundMessage`

| Campo | Tipo | Regras |
|---|---|---|
| `provider` | varchar | `EVOLUTION` |
| `instance` | varchar | instância da Evolution |
| `provider_message_id` | varchar(128) | id da mensagem no WhatsApp; `CHECK <> ''` |
| — | | `UNIQUE (provider, instance, provider_message_id)` → idempotência do webhook |
| `message_type` | varchar | `TEXT` \| `AUDIO` \| `IMAGE` \| `UNSUPPORTED`; `CHECK` |
| `sender_phone` | varchar | E.164 (vazio se não resolvido) |
| `user` | FK User, null | resolvido pelo telefone; `SET_NULL` |
| `text` | text | somente para `READY` (vazio nos demais) |
| `provider_timestamp` | timestamptz, null | horário da mensagem no provider |
| `status` | varchar | `RECEIVED` \| `IGNORED` \| `READY` \| `PROCESSED` \| `FAILED`; `CHECK`; índice `(status, received_at)` |
| `ignore_reason` | varchar | motivo de `IGNORED` (ver `WHATSAPP.md §4`) |
| `error_code` | varchar | código interno, sem dados sensíveis (`AI_*`, `REJECTED_*`, `REPLY_SEND_FAILED`, ...) |
| `intent` | varchar | intenção interpretada (diagnóstico) |
| `transaction` | FK Transaction, null | transação criada pela mensagem; `SET_NULL` |
| `received_at`, `updated_at`, `processed_at` | timestamptz | |

Payload bruto **não** é armazenado (contém `apikey` da instância e dados pessoais).
Mídias **não** ficam no banco (Supabase Storage, V1.4). RLS habilitado pelo mesmo
`post_migrate` das demais tabelas.

Opcional/recomendado: registro da interpretação (`intent`, modelo, latência, tokens,
resultado da validação) ligado à `InboundMessage`, para auditoria e melhoria de prompts.

## 3. Supabase

- Django conecta com credenciais de banco (role com ownership das tabelas), via
  conexão direta ou **session pooler**. Se usar o transaction pooler (porta 6543):
  `CONN_MAX_AGE=0` e `DISABLE_SERVER_SIDE_CURSORS=True`.
- `sslmode=require` em ambientes remotos.
- **Data API (PostgREST) do Supabase**: as tabelas criadas pelo Django no schema `public`
  podem ficar expostas pela API REST do Supabase. Implementado: após cada `migrate`, um handler
  `post_migrate` (`common/db.py`) habilita RLS sem policies em todas as tabelas do schema
  (recurso padrão do PostgreSQL; o role dono das tabelas, usado pelo Django, não é afetado;
  desligável com `DB_ENABLE_RLS=False`). Recomendado também desabilitar a exposição do schema
  na Data API no painel. Ver `SECURITY.md`.
- Chaves `anon`/`service_role` do Supabase nunca vão para cliente algum; `service_role`
  só no servidor e só quando o Storage for usado (V1.4).
- Backups: usar os backups gerenciados do Supabase; confirmar plano/retenção antes da produção.

## 4. Migrations

- Uma migration por mudança lógica; revisar SQL gerado (`sqlmigrate`) quando houver constraints.
- Catálogo inicial de categorias via `python manage.py seed_categories` (não é migration:
  idempotente por `slug`, preserva alterações do administrador).
- Migrations destrutivas (drop de coluna/tabela com dados) exigem plano explícito no PR.
