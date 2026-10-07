# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).
Versões seguem o `ROADMAP.md`. Toda mudança de comportamento, contrato (IA, webhook,
schema) ou decisão arquitetural deve gerar entrada em `Unreleased`.

## [Unreleased]

### Added — V1.4: comprovantes por imagem
- `common/images.py` (Pillow 12.3): formato pelos bytes, limites de bytes/pixels, EXIF/GPS removidos, SHA-256.
- `common/storage.py`: cliente REST mínimo do Supabase Storage (bucket privado, sem sobrescrita);
  comando `storage_check [--write-test]`.
- `apps/ai/vision.py` + contrato `receipt-v1.4` (1 chamada de visão, `detail=high`, schema estrito).
- `apps/whatsapp/image.py`, `actions.plan_receipt`, `ReceiptAttachment` (migration 0004) e
  `pipeline.commit_plan` compartilhado por texto/áudio/imagem; transações `source=IMAGE`.
- `EvolutionClient.download_media` (áudio e imagem pelo mesmo caminho seguro).

### Changed
- Imagem de usuário conhecido deixa de ser `IGNORED/MEDIA_NOT_SUPPORTED`.


### Added — V1.3: áudio → transcrição → pipeline textual
- `apps/ai/transcription.py` (1 chamada, `language="pt"`, modelo `OPENAI_TRANSCRIPTION_MODEL`,
  timeout `OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS`, erros sanitizados, logs só com métricas).
- `EvolutionClient.download_audio` via `getBase64FromMediaMessage` por id, leitura com teto e base64 validado.
- `apps/whatsapp/audio.py`: limites (`AUDIO_MAX_BYTES`, `AUDIO_MAX_SECONDS`), formato pelos bytes
  (`common/media.py`), promoção condicional RECEIVED → READY e o pipeline textual existente.
- Transações vindas de áudio com `source=AUDIO`; respostas continuam em texto.

### Changed
- Áudio de usuário conhecido deixa de ser `IGNORED/MEDIA_NOT_SUPPORTED` (imagem continua).
- Teste de privacidade do contexto passou a usar asserção estrutural (a checagem por substring do pk era instável).


### Fixed — auditoria V1.0–V1.2
- O service e o Django Admin de famílias não permitem mais criar/manter membros sem o ADMIN
  obrigatório nem remover o último ADMIN isoladamente.
- Strings monetárias em notação científica agora são rejeitadas na fronteira `to_decimal`.
- Relatório resumido em `docs/AUDIT-1.md`; a validação real do fluxo MEMBER continua pendente.

### Added — V1.2: acesso financeiro familiar (ADMIN somente leitura)
- Status: implementação e validação automatizada COMPLETE; validação real ADMIN COMPLETE;
  validação real MEMBER PENDING_LIVE_VALIDATION (sem segundo número físico).
- `transactions.scopes`: `ReadScope` (SELF / FAMILY / FAMILY_MEMBER) criado só após autorização
  (`families.policies.can_view_family`, papel atual); selectors aceitam `scope=`.
- `get_member_totals` (ranking por pessoa em um GROUP BY); top/listagem familiar com nome do membro.
- Resolução de membro por nome restrita à família (sem acento/maiúscula; ambiguidade pede nome completo).
- Contrato de IA v1.2: `scope`, `target_member_name`, `query_kind` `RANKING`; contexto de
  follow-up inclui escopo e nome resolvido (sem permissão).
- Respostas locais para família/membro/ranking e negações discretas; mutações citando terceiros recusadas.
- Django Admin de famílias: troca de ADMIN segura no formulário da família; ADMIN não pode ser
  rebaixado/excluído isoladamente; exclusão em massa de memberships desativada.

### Changed
- Regra de visibilidade familiar documentada como snapshot `Transaction.family` (substitui a
  regra provisória da V1.0). Testes de contrato atualizados para os campos da v1.2.


### Added — V1.1: edição, desfazer e consultas avançadas
- `update_transaction` (dono, transação ativa, campos financeiros; regras da criação reaplicadas)
  e `undo_last_change` (pilha CREATE/UPDATE; undo de criação = soft delete; sem redo).
- `TransactionChange`: auditoria de CREATE/UPDATE/DELETE/UNDO com antes/depois, autor, origem;
  `reverts` one-to-one impede desfazer duas vezes. Admin somente leitura.
- Selectors `find_transactions` (referência restrita ao usuário, 90 dias) e `compare_periods`
  (diferença e percentual em Decimal, ROUND_HALF_UP).
- Contrato de IA v1.1: `UPDATE_TRANSACTION`, `UNDO_TRANSACTION`, `query_kind`
  TOP/BREAKDOWN/LIST/COMPARE, `follow_up`, `limit`, período B, objetos `reference`/`updates`.
- `ConversationContext` (TTL `CONVERSATION_CONTEXT_TTL_MINUTES`, padrão 20) para follow-ups,
  enviado à IA como mensagem `developer` estruturada.
- Respostas locais para edição, desfazer, ranking, agrupamento, listagem e comparação.

### Changed
- Mutações por usuário serializadas (lock da linha do usuário); fixtures do contrato atualizadas
  para v1.1; fixtures do pipeline movidas para `apps/whatsapp/tests/conftest.py`.


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
