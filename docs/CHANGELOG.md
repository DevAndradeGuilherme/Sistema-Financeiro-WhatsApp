# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).
Versões seguem o `ROADMAP.md`. Toda mudança de comportamento, contrato (IA, webhook,
schema) ou decisão arquitetural deve gerar entrada em `Unreleased`.

## [Unreleased]

### Added — pipeline textual com IA (V1.0 funcionalmente concluída)
- App `ai`: SDK oficial `openai` 2.54 (Responses API), Structured Outputs estrito, `store=False`,
  uma chamada por mensagem (`max_retries=0`), contrato `v1.0` (ids de categoria do catálogo,
  valor como string decimal), erros mapeados para códigos sanitizados, logs só com métricas.
- `whatsapp.pipeline`/`actions`/`messages`: validação Django da proposta, criação via
  `create_transaction`, consultas `TOTAL`/`BALANCE` via selectors, respostas locais em pt-BR,
  efeito + `PROCESSED` atômicos, envio fora da transação.
- `Category.is_fallback` com constraints (um por tipo, ativa, não `BOTH`); seed marca fallback
  sem sobrescrever escolha do admin.
- `InboundMessage.intent` e `InboundMessage.transaction` (diagnóstico); `common.formatting` (BRL, períodos).
- `OPENAI_REASONING_EFFORT` opcional.

### Removed
- Eco técnico `WHATSAPP_DEV_ECHO` ("Mensagem recebida.").


### Added — transporte WhatsApp via Evolution API 2.3.7 (V1.0, parcial)
- App `whatsapp`: webhook `POST /api/v1/webhooks/evolution/` autenticado por `X-Webhook-Secret`,
  parser de `messages.upsert` para `NormalizedMessage` (TEXT/AUDIO/IMAGE/UNSUPPORTED),
  `InboundMessage` com unicidade `(provider, instance, provider_message_id)`, estados
  RECEIVED/IGNORED/READY/PROCESSED/FAILED e motivos de descarte.
- Filtros: `fromMe`, grupos, broadcast, remetente não resolvido/desconhecido/inativo, mídia
  ainda não suportada, tipos não suportados. Usuário desconhecido nunca é criado.
- `EvolutionClient` (stdlib `urllib`): `send_text`, estado da instância, `set_webhook`/`find_webhook`,
  timeout e erros tipados. Comandos `evolution_check`, `evolution_set_webhook`, `evolution_send_test`.
- Ponto de integração `pipeline.handle_ready_message`; eco técnico opcional `WHATSAPP_DEV_ECHO`
  (proibido em produção).
- Admin somente leitura de mensagens; bloqueio de rede real na suíte de testes.


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
