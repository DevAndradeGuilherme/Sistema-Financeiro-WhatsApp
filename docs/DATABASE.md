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
- Testes e desenvolvimento local usam PostgreSQL real (nunca SQLite).

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
| `family` | FK Family | `PROTECT` |
| `user` | FK User | `UNIQUE` (um usuário, uma família — decisão inicial) |
| `role` | varchar | `ADMIN` \| `MEMBER`; `CHECK` |
| `created_at` | timestamptz | |

### `categories.Category`

| Campo | Tipo | Regras |
|---|---|---|
| `name` | varchar | |
| `slug` | varchar | `UNIQUE`; chave estável no contrato com a IA; não mudar após uso |
| `kind` | varchar | `EXPENSE` \| `INCOME`; `CHECK` |
| `aliases` | `text[]` (ArrayField) | default vazio |
| `is_active` | bool | default true |
| `is_fallback` | bool | no máximo um `true` por `kind` (`UniqueConstraint` parcial) |
| `sort_order` | int | |

### `categories.Subcategory`

| Campo | Tipo | Regras |
|---|---|---|
| `category` | FK Category | `PROTECT` |
| `name` | varchar | |
| `slug` | varchar | `UNIQUE (category, slug)` |
| `aliases` | `text[]` | |
| `is_active` | bool | |

### `transactions.Transaction`

| Campo | Tipo | Regras |
|---|---|---|
| `id` | bigint ou UUID | PK |
| `owner` | FK User | `PROTECT`; índice |
| `type` | varchar | `EXPENSE` \| `INCOME`; `CHECK` |
| `amount` | `NUMERIC(14,2)` | `CHECK (amount > 0)` |
| `category` | FK Category | `PROTECT` |
| `subcategory` | FK Subcategory, null | `PROTECT`; pertinência à categoria validada no service |
| `description` | varchar(255) | pode ser vazio |
| `transaction_date` | date | data financeira |
| `source` | varchar | `WHATSAPP_TEXT` \| `WHATSAPP_AUDIO` \| `WHATSAPP_IMAGE` \| `ADMIN` |
| `idempotency_key` | varchar, null | `UNIQUE` (nulos permitidos). Ex.: `whatsapp:<instance>:<message_id>` |
| `created_at`, `updated_at` | timestamptz | |
| `deleted_at` | timestamptz, null | soft delete |

Índices: `(owner, transaction_date)` e `(owner, category, transaction_date)`, considerando
índice parcial `WHERE deleted_at IS NULL` quando o volume justificar.

Querysets: o manager padrão não deve esconder registros excluídos de forma implícita no
Admin; selectors de domínio **sempre** filtram `deleted_at IS NULL` explicitamente ou via
queryset method nomeado (`.active()`).

### `whatsapp.InboundMessage`

| Campo | Tipo | Regras |
|---|---|---|
| `instance` | varchar | instância da Evolution |
| `external_message_id` | varchar | id da mensagem no WhatsApp |
| — | | `UNIQUE (instance, external_message_id)` → idempotência do webhook |
| `sender_phone` | varchar | E.164 normalizado |
| `user` | FK User, null | resolvido pelo telefone; `SET_NULL` |
| `message_type` | varchar | `TEXT` \| `AUDIO` \| `IMAGE` \| `OTHER` |
| `text` | text, null | texto ou transcrição |
| `status` | varchar | `RECEIVED` \| `PROCESSING` \| `PROCESSED` \| `IGNORED` \| `FAILED` |
| `error_code` | varchar, null | código interno, sem dados sensíveis |
| `received_at`, `processed_at` | timestamptz | |

Payload bruto: armazenar apenas se necessário para debug, com política de retenção
definida antes (contém dados pessoais). Mídias **não** ficam no banco (Supabase Storage, V1.4).

Opcional/recomendado: registro da interpretação (`intent`, modelo, latência, tokens,
resultado da validação) ligado à `InboundMessage`, para auditoria e melhoria de prompts.

## 3. Supabase

- Django conecta com credenciais de banco (role com ownership das tabelas), via
  conexão direta ou **session pooler**. Se usar o transaction pooler (porta 6543):
  `CONN_MAX_AGE=0` e `DISABLE_SERVER_SIDE_CURSORS=True`.
- `sslmode=require` em ambientes remotos.
- **Data API (PostgREST) do Supabase**: as tabelas criadas pelo Django no schema `public`
  podem ficar expostas pela API REST do Supabase. Obrigatório: desabilitar a exposição
  do schema na Data API **e/ou** habilitar RLS sem policies em todas as tabelas
  (o role dono das tabelas, usado pelo Django, não é afetado). Ver `SECURITY.md`.
- Chaves `anon`/`service_role` do Supabase nunca vão para cliente algum; `service_role`
  só no servidor e só quando o Storage for usado (V1.4).
- Backups: usar os backups gerenciados do Supabase; confirmar plano/retenção antes da produção.

## 4. Migrations

- Uma migration por mudança lógica; revisar SQL gerado (`sqlmigrate`) quando houver constraints.
- Catálogo inicial de categorias via data migration idempotente (por `slug`).
- Migrations destrutivas (drop de coluna/tabela com dados) exigem plano explícito no PR.
