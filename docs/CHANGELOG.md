# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).
Versões seguem o `ROADMAP.md`. Toda mudança de comportamento, contrato (IA, webhook,
schema) ou decisão arquitetural deve gerar entrada em `Unreleased`.

## [Unreleased]

### Added — núcleo do domínio financeiro (V1.0, parcial)
- Projeto Django 5.2 LTS (Python 3.12) com settings `base`/`runtime`/`test` por variáveis de
  ambiente; guardas para `DJANGO_ENV=production`; sem fallback para SQLite.
- Apps `accounts` (User customizado, telefone E.164 único), `families` (Family, FamilyMember,
  um ADMIN por família, policy de visibilidade), `categories` (Category/Subcategory com
  `kind` EXPENSE/INCOME/BOTH, aliases, seed idempotente `seed_categories`) e `transactions`
  (Transaction UUID, Decimal, soft delete, idempotency key; `create_transaction`,
  `soft_delete_transaction`; selectors de listagem, totais, por categoria e top despesas).
- `common`: normalização de telefone, helpers de dinheiro/data, health check `/health/`,
  RLS automático pós-migrate.
- Django Admin com restrições: catálogo só por superusuário, transações somente leitura,
  staff não altera privilégios nem contas de superusuário.
- Testes (pytest, PostgreSQL local descartável) e lint (ruff).
- Dockerfile (Gunicorn, usuário não-root, healthcheck) e `compose.yaml`.

### Changed
- Documentação alinhada à implementação: `kind` BOTH, valores de `source`, `Transaction.family`,
  ADMIN único por família, settings divididos, estratégia de banco de testes.


### Added
- Fundação documental: `AGENTS.md`, `README.md` e `docs/` (arquitetura, domínio, banco,
  contrato de IA, WhatsApp, segurança, testes, deploy, roadmap).
- Arquivos auxiliares: `.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example`.

### Changed
- `README.md` reescrito em UTF-8 (original estava em UTF-16).
