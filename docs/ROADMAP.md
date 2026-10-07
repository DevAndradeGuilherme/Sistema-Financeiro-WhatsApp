# Roadmap

Versão atual: **V1.4 concluída** (comprovantes por imagem validados ponta a ponta em DEV, 2026-10-07).
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
- [x] Armazenamento em Supabase Storage privado, sem EXIF; `ReceiptAttachment` com metadados.
- [x] Extração por visão (1 chamada, schema estrito) + validação Django + `create_transaction`
  (`source=IMAGE`); registro direto quando inequívoco, "desfazer" disponível.
- [x] Validação real (2026-10-07): recibo de despesa, Pix enviado e imagem não financeira pelo WhatsApp
  (Pix recebido validado por smoke sintético e testes).
- RETENTION_POLICY = PENDING_PRODUCT_DECISION.

## V1.5 — Análises

- Resumos, breakdown por categoria (`CATEGORY_BREAKDOWN`), comparação de períodos,
  maiores gastos, análises familiares autorizadas.
- Preparação para relatórios posteriores.

## Fora do roadmap atual

- Dashboard/frontend próprio (Django Admin é suficiente até prova em contrário).
- Auto-cadastro de usuários via WhatsApp.
- Múltiplas moedas, orçamentos/metas, contas bancárias, integrações bancárias.
- Filas/caches externos (Celery, Redis) e frameworks de agentes.
