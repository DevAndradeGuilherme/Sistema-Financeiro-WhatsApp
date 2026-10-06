# Roadmap

Versão atual: **pré-V1.0** (fundação documental concluída; nenhuma funcionalidade implementada).

Regra: implementar somente a versão corrente. Itens de versões futuras só influenciam
o desenho quando registrado aqui (ex.: estrutura de família desde a V1.0).
Ao concluir uma versão, atualizar a linha "Versão atual" e o `CHANGELOG.md`.

## V1.0 — Fundação funcional

- Projeto Django, settings por env, Docker, Gunicorn, Nginx (alvo de `DEPLOYMENT.md`).
- Supabase PostgreSQL; testes com PostgreSQL.
- `accounts`: User customizado com telefone E.164.
- `categories`: categorias/subcategorias com aliases, catálogo inicial, Django Admin.
- `families`: **apenas models e Admin** (sem funcionalidade exposta no canal).
- `transactions`: criação de despesas e receitas; consultas de totais próprios por
  tipo/categoria/subcategoria/período.
- `whatsapp`: webhook Evolution autenticado, idempotência, identificação por telefone,
  envio de respostas.
- `ai`: interpretação textual estruturada (`CREATE_TRANSACTION`, `QUERY_TRANSACTIONS`,
  `HELP`, `UNKNOWN`).
- Django Admin para usuários, categorias, transações e mensagens.

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
