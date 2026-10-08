# Roadmap

Versão atual: **V1.5 concluída** (análises financeiras validadas ponta a ponta pelo WhatsApp em DEV,
2026-10-07).
V1.4 concluída (comprovantes por imagem validados ponta a ponta em DEV, 2026-10-07).
V1.2: validação real do fluxo MEMBER segue `PENDING_LIVE_VALIDATION` (ver status da V1.2).
Pendência operacional antes de produção: Nginx/TLS, host/domínio e procedimento de deploy —
produção **não** está pronta.

Regra: implementar somente a versão corrente. Itens de versões futuras só influenciam
o desenho quando registrado aqui (ex.: estrutura de família desde a V1.0).
Ao concluir uma versão, atualizar a linha "Versão atual" e o `CHANGELOG.md`.

## V1.0 — Fundação funcional

- [x] Projeto Django, settings por env, Docker, Gunicorn.
- [ ] Nginx/TLS e procedimento de produção (`DEPLOYMENT.md`) — pendência operacional.
- [x] Supabase PostgreSQL (migrations aplicadas em dev); testes com PostgreSQL local.
- [x] `accounts`: User customizado com telefone E.164.
- [x] `categories`: categorias/subcategorias com aliases, catálogo inicial, Django Admin.
- [x] `families`: models, services, policy de visibilidade e Admin (sem funcionalidade no canal).
- [x] `transactions`: criação de despesas e receitas, soft delete; selectors determinísticos
  (período, total, por categoria, top despesas).
- [x] `whatsapp` (transporte): webhook Evolution autenticado, `InboundMessage`, idempotência,
  filtros (fromMe/grupo/mídia), identificação por telefone, client de envio, comandos operacionais.
- [x] `whatsapp` (pipeline): `READY` → IA → validação → services/selectors → resposta → `PROCESSED`.
- [x] `ai`: interpretação textual estruturada (`CREATE_TRANSACTION`, `QUERY_TRANSACTIONS`,
  `HELP`, `UNKNOWN`) via Responses API + schema estrito.
- [x] Fallback explícito de categoria (`is_fallback`).
- [x] Django Admin para usuários, famílias, categorias e transações.
- [x] Django Admin para mensagens (somente leitura).

Pronto quando: "10 reais de gasolina" cria transação correta e "quanto gastei de transporte
esse mês?" retorna o total correto, ambos ponta a ponta via WhatsApp, com testes cobrindo
`TESTING.md §3`.

## V1.1 — Edição, desfazer e consultas avançadas

- [x] `UPDATE_TRANSACTION` e `UNDO_TRANSACTION` (somente transações próprias; auditoria em
  `TransactionChange`; undo de criação = soft delete).
- [x] Consultas avançadas: maiores gastos (`TOP`), por categoria (`BREAKDOWN`), listagem curta
  (`LIST`), comparação de períodos (`COMPARE`), mantendo `TOTAL`/`BALANCE`.
- [x] Contexto conversacional mínimo (`ConversationContext`, TTL 20 min) para follow-ups.

## V1.2 — Famílias

- [x] Famílias, membros, papéis ADMIN/MEMBER ativos no canal (ADMIN = somente leitura sobre terceiros).
- [x] Consultas familiares (total, receita, saldo, por categoria, maiores gastos, comparação,
  ranking por pessoa) e por membro, com follow-ups.
- [x] Regras de isolamento e autorização (`DOMAIN.md §6`) testadas; snapshot `Transaction.family`.

Status da V1.2 (2026-10-07):

| Item | Status |
|---|---|
| IMPLEMENTATION_STATUS | COMPLETE |
| AUTOMATED_VALIDATION | COMPLETE (ADMIN/MEMBER distintos, famílias isoladas, mutações em terceiros, perda de papel, contexto, ambiguidade, snapshot) |
| ADMIN_LIVE_VALIDATION | COMPLETE (número real como ADMIN da "Família DEV": consultas familiares/membro/ranking/top/comparação e negação de UPDATE/UNDO em terceiro) |
| MEMBER_LIVE_VALIDATION | PENDING_LIVE_VALIDATION — requer um segundo número físico (o MEMBER da Família DEV é fictício, sem telefone) |

A V1.2 **não** está integralmente validada em ambiente real até o fluxo MEMBER ser executado
por um segundo número: "20 reais de alimentação", "quanto gastei esse mês?", "quanto minha
família gastou esse mês?" (negar) e "quanto <ADMIN> gastou esse mês?" (negar).

## V1.3 — Áudio

- [x] Recebimento de áudio (nota de voz e arquivo `audioMessage`), download seguro pela Evolution, transcrição (`gpt-transcribe` configurável).
- [x] Reaproveitamento integral do pipeline textual (`WHATSAPP.md §7`); sem armazenamento de mídia.
- [x] Validação real (2026-10-07): 5 áudios (despesa, receita, consulta, edição, desfazer) + contexto texto→áudio.

## V1.4 — Imagens

- [x] Comprovantes, recibos, cupons e Pix (JPEG/PNG/WEBP/GIF estático; PDF fora do escopo).
- [x] ~~Armazenamento em Supabase Storage privado~~ substituído (2026-10-08) por processamento
  efêmero em memória, sem EXIF; `ReceiptAttachment` só com metadados técnicos.
- [x] Extração por visão (1 chamada, schema estrito) + validação Django + `create_transaction`
  (`source=IMAGE`); registro direto quando inequívoco, "desfazer" disponível.
- [x] Validação real (2026-10-07): recibo de despesa, Pix enviado e imagem não financeira pelo WhatsApp
  (Pix recebido validado por smoke sintético e testes).
- IMAGE_RETENTION = NONE (imagens não são retidas); demais retenções seguem pendentes.
- IMAGE_NO_STORAGE_SMOKE_VALIDATION = COMPLETE (2026-10-08: pipeline real + OpenAI Vision real +
  Supabase DEV revertido; recibo sintético virou EXPENSE R$ 23,45 e paisagem NOT_A_RECEIPT, ambos
  com metadados e storage_path NULL; único host acessado: api.openai.com).
- IMAGE_NO_STORAGE_LIVE_VALIDATION = COMPLETE (2026-10-08: um recibo real pelo WhatsApp DEV com o
  app sem `SUPABASE_*`; mensagem PROCESSED, 1 download, 1 chamada Vision, despesa `source=IMAGE`
  conferida por SQL, `ReceiptAttachment` PROCESSED com SHA-256 e `storage_*` NULL, nenhum
  byte/base64 no banco, nenhum acesso ao Storage, resposta enviada pela Evolution).

## V1.5 — Análises

- [x] Resumo do período (`PERIOD_SUMMARY`): receitas, despesas, saldo, contagens, gasto médio,
  maior gasto/receita, 3 principais categorias, % da renda gasta e % economizado.
- [x] Evolução mensal (`MONTHLY_TREND`): gastos, receitas ou saldo mês a mês (1–12 meses, padrão 6).
- [x] Variação por categoria (`CATEGORY_TREND`): aumentos, reduções ou todas as variações.
- [x] `TOP` de receitas ("maior entrada"); breakdown/comparação/maiores gastos já existentes (V1.1).
- [x] Análises familiares autorizadas pelos mesmos escopos da V1.2 (sem caminho novo de autorização).
- [x] Índice `(family, transaction_date)` (migration `transactions/0003`, aplicada no Supabase DEV).
- Relatórios persistidos/PDF continuam fora do escopo (nenhuma tabela de relatório criada).

Status da V1.5 (2026-10-07):

| Item | Status |
|---|---|
| IMPLEMENTATION_STATUS | COMPLETE |
| AUTOMATED_VALIDATION | COMPLETE (768 testes; analytics de domínio + pipeline V1.5) |
| OPENAI_SMOKE_VALIDATION | COMPLETE (9 frases V1.5 + 4 frases antigas interpretadas corretamente) |
| INTEGRATED_LOCAL_VALIDATION | COMPLETE (pipeline real + OpenAI + Supabase DEV, dados sintéticos revertidos, sem envio) |
| V1_5_LIVE_VALIDATION | COMPLETE (8 mensagens reais do ADMIN DEV: resumo, evolução 6 meses, aumentos por categoria, % da renda, resumo/evolução da família, resumo da Maria DEV e follow-up "e mês passado?"; valores conferidos por SQL, nenhuma mutação) |
| MEMBER_LIVE_VALIDATION | PENDING_LIVE_VALIDATION (herdado da V1.2) |
| RETENTION_POLICY | Imagens: `IMAGE_RETENTION = NONE` (2026-10-08). `InboundMessage`, metadados, `Transaction` e auditoria: PENDING_PRODUCT_DECISION |

Limitação conhecida: `CATEGORY_TREND` sem período explícito compara o mês corrente até hoje com
o mês anterior completo (mesma semântica do `COMPARE`); ver `DOMAIN.md §4`.

## Fora do roadmap atual

- Dashboard/frontend próprio (Django Admin é suficiente até prova em contrário).
- Auto-cadastro de usuários via WhatsApp.
- Múltiplas moedas, orçamentos/metas, contas bancárias, integrações bancárias.
- Filas/caches externos (Celery, Redis) e frameworks de agentes.
