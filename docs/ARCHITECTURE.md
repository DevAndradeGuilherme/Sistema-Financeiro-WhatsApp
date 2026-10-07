# Arquitetura

Regras de negócio estão em [`DOMAIN.md`](DOMAIN.md); esquema de dados em
[`DATABASE.md`](DATABASE.md). Este documento trata de estrutura, camadas, fronteiras e fluxos.

## 1. Estilo arquitetural

**Monólito modular em Django.** Um único deploy, um único banco, apps Django com
fronteiras explícitas.

Explicitamente fora do MVP: microserviços, Celery, Redis, LangChain, sistemas de agentes,
filas externas, dashboard próprio. Adoção futura exige registro na seção 8.

## 2. Divisão de responsabilidades

| Camada | Responsável por | Nunca faz |
|---|---|---|
| **IA (OpenAI)** | Interpretar texto livre em uma proposta estruturada (intenção + campos) | Acessar banco, gerar SQL, somar valores, decidir permissões, criar categorias |
| **Django** | Validar a proposta, resolver referências (categoria, usuário, período), autorizar, orquestrar, formatar resposta | Confiar em campos de autorização vindos da IA ou do payload |
| **PostgreSQL** | Persistir, garantir integridade (constraints, unicidade), agregar (`SUM`, `GROUP BY`) | — |
| **WhatsApp (Evolution)** | Transportar mensagens de entrada e saída | Conter regra de negócio |

Totais financeiros são sempre calculados por agregação no banco via ORM
(`Sum`, `Count`), retornando `Decimal`. Nunca somar em Python a partir de valores
fornecidos pela IA.

## 3. Módulos (apps Django)

```
config/                 settings, urls raiz, wsgi
apps/
  accounts/             User customizado (AbstractUser) + telefone E.164
  families/             Family, FamilyMember, policies de visibilidade
  categories/           Category, Subcategory, aliases; catálogo para a IA
  transactions/         Transaction; services (escrita) e selectors (leitura/agregação)
  ai/                   cliente OpenAI, schemas de saída, prompts
  whatsapp/             webhook, InboundMessage, cliente Evolution, orquestração
common/                 helpers puros: money, dates, phone, logging/redaction
tests/                  testes de integração ponta a ponta
docs/
```

### Responsabilidade e dependências permitidas

| App | Responsabilidade | Pode importar |
|---|---|---|
| `common` | Funções puras sem regra de negócio | nada do projeto |
| `accounts` | Identidade, telefone canônico | `common` |
| `families` | Grupos familiares, papéis, **regras de visibilidade entre usuários** | `accounts`, `common` |
| `categories` | Catálogo fixo de categorias/subcategorias | `common` |
| `transactions` | Domínio financeiro: criar, editar, desfazer, consultar, agregar | `accounts`, `families`, `categories`, `common` |
| `ai` | Converter texto + contexto em proposta estruturada | `common` (**não importa models de domínio**) |
| `whatsapp` | Entrada/saída do canal e orquestração do pipeline | todos os anteriores |

Regras:

- Dependências apontam em uma só direção (tabela acima). Ciclos são proibidos.
- Nenhum app de domínio importa `whatsapp`. O domínio não conhece o canal.
- `ai` recebe dados simples (texto, data de referência, lista de categorias como DTO) e
  devolve um objeto de schema validado. É substituível e testável sem banco.
- `transactions` não conhece WhatsApp: a idempotência usa um `idempotency_key`
  agnóstico de canal (ver `DATABASE.md`).

### Organização interna de um app

Criar apenas os arquivos necessários:

- `models.py` — estrutura e constraints; sem lógica de fluxo.
- `services.py` — operações de escrita; recebem `actor` explícito; validam e autorizam.
- `selectors.py` — leitura e agregação; sempre recebem `actor` e aplicam escopo.
- `policies.py` — funções puras de autorização (`can_view`, `can_modify`...).
- `admin.py` — Django Admin.
- `tests/` — testes do app.

Views e o webhook são finos: parse/validação de entrada → service/selector → resposta.
Não usar signals para regra de negócio. Não criar repositórios genéricos, managers
"inteligentes" ou camadas de interface sem necessidade concreta.

## 4. Fluxo de mensagem (texto)

```
Evolution ──POST──> /webhooks/evolution/<...>   (whatsapp)
  1. Autentica webhook (segredo).                         → 401/403 se inválido
  2. Filtra eventos irrelevantes (fromMe, grupo, status).  → 200 no-op
  3. Persiste InboundMessage (unique por message id).      → duplicada: 200 no-op
  4. Resolve remetente → User pelo telefone E.164.         → desconhecido: não processa
  5. Monta contexto: data de referência (America/Sao_Paulo) + catálogo de categorias ativas.
  6. ai.interpret(texto, contexto) → Interpretation (schema estruturado).
  7. Validação Django: tipos, valor Decimal > 0, data, categoria existente/ativa,
     subcategoria pertencente à categoria, período coerente.
  8. Despacho por intenção:
       CREATE_TRANSACTION  → transactions.services.create_transaction(actor, ...)
       QUERY_TRANSACTIONS  → transactions.selectors.* (escopo autorizado) → agregação SQL
       UPDATE/UNDO         → services com policy can_modify (V1.1)
       HELP / UNKNOWN      → resposta fixa
  9. Formata resposta pt-BR (R$ 1.234,56; datas dd/mm/aaaa).
 10. Envia via cliente Evolution; atualiza status da InboundMessage.
```

Processamento **síncrono** dentro da requisição do webhook no MVP (sem fila).
Timeouts obrigatórios em chamadas externas (OpenAI, Evolution). Se a latência virar
problema, a evolução prevista é um worker por management command que processa
`InboundMessage` pendentes com `SELECT ... FOR UPDATE SKIP LOCKED` — ainda sem Redis/Celery.
Essa mudança exige registro na seção 8.

### Áudio (V1.3) e imagem (V1.4)

```
Áudio:  Evolution → download da mídia → transcrição → texto → passo 5 em diante (mesmo pipeline)
Imagem: Evolution → download → (Supabase Storage) → extração multimodal estruturada
        → mesma validação Django → mesmo transactions.services.create_transaction
```

Proibido criar regras financeiras paralelas para áudio ou imagem. O que muda é apenas
a etapa que produz o texto/proposta.

## 5. Fluxo de consulta

1. IA propõe: intenção, tipo, categoria, período (`start_date`/`end_date`), escopo, alvo.
2. Django resolve categoria por chave do catálogo; período validado (início ≤ fim).
3. Escopo efetivo é calculado por `families.policies` a partir do **ator**:
   - `scope`/`target_user` da IA são apenas pistas sobre *o que o usuário pediu*;
   - se o ator não tem permissão para o escopo pedido, a resposta é negação, nunca fallback
     silencioso para dados de outro usuário.
4. Selector monta queryset já restrito ao escopo autorizado, exclui soft-deleted e agrega no banco.

## 6. Tempo e dinheiro

- `USE_TZ = True`; `TIME_ZONE = "America/Sao_Paulo"` (configurável por env).
- "Hoje" do usuário = data local em `America/Sao_Paulo`, nunca `date.today()` do servidor UTC.
  Centralizar em `common` (ex.: `common.dates.today_local()`).
- Dinheiro: `Decimal`, quantização em 2 casas com `ROUND_HALF_UP`; helpers em `common.money`.
- Moeda única: BRL.

## 7. Configuração

- Tudo sensível ou dependente de ambiente vem de variáveis de ambiente (`.env.example`).
- `OPENAI_MODEL` obrigatório por env; sem valor default hardcoded em código de negócio.
- Settings em `config/settings/`: `base.py` (comum, sem banco), `runtime.py` (app/servidor,
  banco `DB_*`) e `test.py` (suíte, banco `TEST_DB_*` local). A divisão existe para que os
  testes nunca usem a configuração de banco da aplicação.
- `DJANGO_ENV=production` ativa guardas: falha se `DEBUG=True`, sem `ALLOWED_HOSTS` ou com
  `SECRET_KEY` fraca; liga cookies seguros, HSTS e redirect HTTPS (exceto `/health/`).
- `common` é um app Django (sem models) para poder registrar o handler de RLS pós-migrate.

## 8. Registro de decisões (ADR resumido)

Formato: `AAAA-MM-DD — decisão — motivo`. Adicionar novas decisões no fim.

- 2026-10-06 — Monólito modular Django, sem fila/cache externos no MVP — simplicidade operacional.
- 2026-10-06 — User customizado desde a primeira migration — trocar depois é custoso no Django.
- 2026-10-06 — Valor monetário sempre positivo; sinal definido por `type` (EXPENSE/INCOME) — evita ambiguidade.
- 2026-10-06 — Idempotência por `idempotency_key` único em `Transaction` + unicidade em `InboundMessage` — dupla proteção, domínio independente do canal.
- 2026-10-06 — IA devolve `amount` como string decimal, não número JSON — evita float na fronteira.
- 2026-10-06 — Processamento síncrono no webhook no MVP — sem infraestrutura de fila.
- 2026-10-06 — Estrutura de família criada cedo (V1.0), funcionalidades liberadas na V1.2 — evita migração de dados posterior.
- 2026-10-06 — Testes rodam contra PostgreSQL, não SQLite — constraints e tipos precisam ser os reais.
- 2026-10-06 — Python 3.12, Django 5.2 LTS, psycopg 3; dependências fixadas em `requirements*.txt` — LTS até 2028.
- 2026-10-06 — Configuração de banco por `DB_*` (não `DATABASE_URL`) — já era o contrato documentado no `.env.example`; evita dependência de parser de URL.
- 2026-10-06 — `python-dotenv` para ler `.env` localmente — variáveis do ambiente sempre têm precedência.
- 2026-10-06 — Testes em PostgreSQL local descartável (container `test-db`), nunca no Supabase — o test runner cria/destroi banco; segurança acima de conveniência.
- 2026-10-06 — RLS sem policies habilitado automaticamente após `migrate` — impede leitura pela Data API do Supabase.
- 2026-10-06 — `Category.kind` aceita `BOTH` — categorias usadas tanto para despesa quanto receita.
- 2026-10-06 — `Transaction.family` = família do dono na criação, derivada pelo service — base para consultas familiares sem permitir atribuição arbitrária.
- 2026-10-06 — Exatamente um ADMIN por família (unique parcial no banco + service/admin) — regra simples e verificável.
- 2026-10-06 — Admin de transações é somente leitura — dados financeiros só mudam pelos services e pelo dono.
- 2026-10-06 — Catálogo inicial por management command idempotente, não data migration — administrador pode alterar o catálogo livremente.
