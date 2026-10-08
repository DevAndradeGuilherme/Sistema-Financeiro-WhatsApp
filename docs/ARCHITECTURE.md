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
  transactions/         Transaction; services (escrita) e selectors (leitura/agregação), analytics (V1.5)
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
     → status READY (texto + usuário). A partir daqui: whatsapp.pipeline.handle_ready_message
  5. Monta contexto: data de referência (America/Sao_Paulo) + catálogo ativo (ids inteiros).
  6. ai.interpret_message(texto, contexto) → Interpretation   [1 chamada OpenAI, sem transação aberta]
  7. whatsapp.actions.plan: validação Django (Decimal, datas ISO, ids do catálogo, fallback).
  8. atomic[ lock da InboundMessage (READY?) → efeito → status PROCESSED ]:
       CREATE_TRANSACTION  → transactions.services.create_transaction(user=remetente, ...)
       UPDATE_TRANSACTION  → selectors.find_transactions (só do remetente) → services.update_transaction
       UNDO_TRANSACTION    → services.undo_last_change(user=remetente)
       QUERY_TRANSACTIONS  → transactions.scopes.resolve (autoriza) → selectors.*(scope=) (SQL)
                             + ConversationContext.remember
       HELP / UNKNOWN      → resposta fixa
  9. Resposta montada localmente (whatsapp.messages + common.formatting).
 10. Envia via cliente Evolution, fora da transação; falha não reexecuta o efeito.
```

Processamento **síncrono** dentro da requisição do webhook no MVP (sem fila).
Timeouts obrigatórios em chamadas externas (OpenAI, Evolution). Se a latência virar
problema, a evolução prevista é um worker por management command que processa
`InboundMessage` pendentes com `SELECT ... FOR UPDATE SKIP LOCKED` — ainda sem Redis/Celery.
Essa mudança exige registro na seção 8.

### Áudio (V1.3) e imagem (V1.4)

```
Áudio (V1.3 ✅): Evolution → download (por id, autenticado) → transcrição (1 chamada) → texto
        → READY → passo 5 em diante (mesmo pipeline; `whatsapp/audio.py`, `ai/transcription.py`)
Imagem (V1.4 ✅): Evolution → download (por id, em memória) → normalização Pillow (sem EXIF)
        → SHA-256/metadados → 1 chamada de visão (schema estrito) → actions.plan_receipt
        → pipeline.commit_plan (transação + metadados do anexo + PROCESSED no mesmo atomic)
        → mesmo transactions.services.create_transaction (`whatsapp/image.py`, `ai/vision.py`)
        → bytes descartados
```

Imagens de comprovante são processadas de forma efêmera, em memória, e **não são retidas**:
nenhum Storage, arquivo temporário, bytes, base64 ou URL. Persistem só a `Transaction` e os metadados técnicos (`ReceiptAttachment`).

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

Análises (V1.5): `PERIOD_SUMMARY`, `MONTHLY_TREND` e `CATEGORY_TREND` seguem o mesmo fluxo.
`apps/transactions/analytics.py` recebe o mesmo `ReadScope` autorizado (nenhum caminho novo de
autorização), agrega no PostgreSQL (`Sum`/`Count` condicionais, `TruncMonth`) e devolve DTOs
imutáveis em `Decimal`; nada é persistido. A resposta é montada localmente em
`whatsapp/messages.py` (fatos, sem aconselhamento) — a IA nunca calcula nem redige valores.

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
- 2026-10-06 — Cliente Evolution com `urllib` da stdlib, sem dependência HTTP nova — poucas chamadas JSON; timeout e erros tratados explicitamente.
- 2026-10-06 — Webhook autenticado por header `X-Webhook-Secret` (suportado em `webhook.headers` na Evolution 2.3.7), nunca segredo na URL.
- 2026-10-06 — Remetente desconhecido não é criado nem recebe resposta — evita spam/enumeração; cadastro via Admin.
- 2026-10-06 — Payload bruto do webhook não é persistido; texto só para mensagens `READY` — minimiza dados sensíveis.
- 2026-10-06 — Erro após a reserva idempotente responde `200` e marca `FAILED` — retry da Evolution seria no-op; reprocessamento manual.
- 2026-10-06 — Pipeline textual síncrono: 1 chamada OpenAI (Responses API, schema estrito, `store=False`, `max_retries=0`) por mensagem; resposta montada pelo Django — custo/latência previsíveis.
- 2026-10-06 — IA referencia categorias por id inteiro do catálogo ativo; Django revalida e usa fallback explícito (`Category.is_fallback`) — nunca texto livre nem nome.
- 2026-10-06 — Efeito financeiro e `PROCESSED` no mesmo `transaction.atomic()` com `select_for_update` da mensagem; chamadas HTTP fora da transação — sem duplicação e sem transação longa.
- 2026-10-06 — Consultas V1.0 sempre do próprio remetente; contrato sem `scope`/`target_user` — consultas familiares ficam para a V1.2.
- 2026-10-06 — `OPENAI_REASONING_EFFORT` opcional (dev: `none` com `gpt-6-luna`) — mesma qualidade nos casos testados, sem tokens de raciocínio.
- 2026-10-06 — Eco técnico `WHATSAPP_DEV_ECHO` removido — substituído pelo pipeline real.
- 2026-10-07 — Auditoria e desfazer via `TransactionChange` (before/after só com campos financeiros, `reverts` one-to-one) — mínimo auditável, sem event sourcing.
- 2026-10-07 — Mutações por usuário serializadas com `SELECT ... FOR NO KEY UPDATE` na linha do usuário — evita corrida update × undo sem bloquear inserts com FK.
- 2026-10-07 — Contexto conversacional em `whatsapp.ConversationContext` (PostgreSQL, TTL 20 min, só a última consulta validada) — sem Redis, sem memória aberta.
- 2026-10-07 — Contrato v1.1 plano + objetos `reference`/`updates`; consultas avançadas como `query_kind` — menos campos que intenções separadas.
- 2026-10-07 — Leitura familiar via `transactions.scopes.ReadScope` (só criável após a policy; selectors aceitam `scope=`) — autorização centralizada, sem selector por `user_id`.
- 2026-10-07 — Dado familiar = snapshot `Transaction.family`; consulta por membro exige membership atual — sem reatribuição retroativa de histórico.
- 2026-10-07 — Nenhuma lista de membros enviada à IA; nome resolvido no Django dentro da família — privacidade e tokens.
- 2026-10-07 — Django Admin: troca de ADMIN só pelo formulário da família (rebaixa antes de promover); ADMIN não pode ser rebaixado/excluído isoladamente com membros na família.
- 2026-10-07 — Áudio = transporte: `audio.py` (download/limites) + `ai.transcription` (1 chamada, `language="pt"`), depois o pipeline textual inalterado — sem segundo domínio.
- 2026-10-07 — Download de mídia só por id via `getBase64FromMediaMessage` (Evolution consulta o próprio banco) — nenhuma URL do webhook é acessada.
- 2026-10-07 — OGG/Opus enviado sem conversão (aceito pela API, validado ao vivo) — sem ffmpeg; formato detectado pelos bytes.
- 2026-10-07 — Áudio aguardando transcrição permanece `RECEIVED`; rejeições de limite usam `PROCESSED` + código — sem novos estados nem migration.
- 2026-10-07 — Imagem = transporte: `image.py` + `ai.vision` (1 chamada, `detail=high`) + `actions.plan_receipt`, gravando pelo mesmo `pipeline.commit_plan` — sem segundo domínio.
- 2026-10-07 — Pillow (dependência nova) só para validar/normalizar e remover metadados; sem OCR local.
- 2026-10-07 — Cliente Storage próprio com stdlib (4 chamadas REST) em vez do SDK Supabase completo; bucket privado, service role só no backend. **Substituída em 2026-10-08** (abaixo).
- 2026-10-07 — Comprovante: registro direto quando inequívoco (sem confirmação), com "desfazer" disponível; incerteza → pede dado em texto.
- 2026-10-07 — Análises V1.5 em `transactions/analytics.py` sobre `ReadScope` (sem selector/autorização novos), calculadas sob demanda e nunca persistidas — sem tabelas de relatório nem snapshots.
- 2026-10-07 — `CATEGORY_TREND` reutiliza a semântica de períodos do `COMPARE` (mês corrente até hoje × mês anterior completo) — consistência entre consultas; limitação conhecida documentada em `DOMAIN.md`.
- 2026-10-07 — Índice `(family, transaction_date)` em `Transaction` (migration `transactions/0003`) — leituras familiares/análises filtram pela família histórica + período.
- 2026-10-08 — Comprovantes sem armazenamento: imagem processada só em memória e descartada; `ReceiptAttachment` vira metadados técnicos gravados após a visão, no mesmo atomic da transação; cliente Storage, `storage_check` e `SUPABASE_*` removidos (sem service role key no runtime) — `IMAGE_RETENTION = NONE`, menos dado sensível e uma credencial de alto privilégio a menos. Colunas `storage_*` mantidas como legado (NULL em novas linhas) até a limpeza manual do bucket DEV.
- 2026-10-08 — Django Admin como interface operacional inicial de usuários, famílias e acessos,
  sem painel próprio. Substitui a troca de papéis por inline (decisão de 2026-10-07) por operação
  explícita de transferência, atômica e serializada pelo lock da família. Inclusão/remoção usam
  o mesmo lock; membership isolado fica somente leitura. LogEntry participa da mesma transação.
  Nenhum schema/dependência novo. Exclusões administrativas de User/Family são bloqueadas;
  desativação preserva identidade e histórico. Ledger e diagnósticos permanecem somente leitura.
