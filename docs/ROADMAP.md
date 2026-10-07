# Roadmap

Versão atual: **V1.0 funcionalmente concluída** (validada ponta a ponta em DEV, 2026-10-06).
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

- `UPDATE_TRANSACTION` e `UNDO_TRANSACTION` (somente transações próprias; soft delete).
- Consultas por categoria e períodos variados; maiores gastos (`TOP_EXPENSES`);
  comparação de períodos (`COMPARE_PERIODS`).
- Contexto conversacional mínimo quando necessário (ex.: referência à última transação).

## V1.2 — Famílias

- Famílias, membros, papéis ADMIN/MEMBER ativos no canal.
- Consultas familiares consolidadas e consultas individuais pelo ADMIN (`FAMILY_QUERY`).
- Regras completas de isolamento e autorização (`DOMAIN.md §6`) testadas ponta a ponta.

## V1.3 — Áudio

- Recebimento de áudio, download da mídia, transcrição.
- Reaproveitamento integral do pipeline textual (`WHATSAPP.md §6`).

## V1.4 — Imagens

- Comprovantes e recibos; armazenamento em Supabase Storage (bucket privado).
- Extração multimodal estruturada; confirmação e registro pelo mesmo domínio financeiro.

## V1.5 — Análises

- Resumos, breakdown por categoria (`CATEGORY_BREAKDOWN`), comparação de períodos,
  maiores gastos, análises familiares autorizadas.
- Preparação para relatórios posteriores.

## Fora do roadmap atual

- Dashboard/frontend próprio (Django Admin é suficiente até prova em contrário).
- Auto-cadastro de usuários via WhatsApp.
- Múltiplas moedas, orçamentos/metas, contas bancárias, integrações bancárias.
- Filas/caches externos (Celery, Redis) e frameworks de agentes.
