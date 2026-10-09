# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).
Versões seguem o `ROADMAP.md`. Toda mudança de comportamento, contrato (IA, webhook,
schema) ou decisão arquitetural deve gerar entrada em `Unreleased`.

## [Unreleased]

### Added — hardening operacional de pré-produção
- Readiness `GET /ready/`: `SELECT 1` no PostgreSQL com `statement_timeout` local de 2 s; `503`
  com corpo fixo quando o banco falha; sem OpenAI/Evolution, sem escrita, sem detalhes internos.
  `/health/` continua liveness pura (sem banco). Ambos isentos do redirect HTTPS.
- Comando `inbound_recovery` (`list`, `inspect`, `resend-reply`, `mark-failed`): diagnóstico de
  mensagens interrompidas com efeito financeiro (por `idempotency_key`/`origin`) e ação segura;
  dry-run por padrão, `--execute` explícito, verificações sob o lock da mensagem. Nunca
  reprocessa nem altera o ledger: reenvia apenas o card de CREATE comitado e inalterado
  (`REPLY_RESEND_PENDING` → `REPLY_RESENT`) ou marca `FAILED` (`STUCK_*`) mensagem travada sem
  efeito, com a resposta de erro técnico já existente. Estados ambíguos são recusados.
- `docs/OPERATIONS.md`: runbook de health/readiness, recuperação, incidentes e política inicial
  de retenção (ledger/auditoria/metadados retidos, `IMAGE_RETENTION = NONE`, `InboundMessage`
  preservada, `ConversationContext` elegível a limpeza futura, logs com objetivo de 30 dias).
- `common.phone.mask_phone` para saídas operacionais.

### Changed
- Gunicorn 23.0.0 → 26.2.0 (23.x sem suporte de segurança upstream; série 26 suportada,
  Python ≥ 3.10). `--no-control-socket` no `CMD`. `packaging` sai de `requirements.txt`
  (não é mais dependência do Gunicorn) e fica fixado em `requirements-dev.txt` (pytest).
- `actions.created_reply` extraído para o pipeline e a recuperação usarem o mesmo card;
  `services.mark_failed` informa se fez a transição. Sem mudança de comportamento ou textos.
- Sem migration, sem dependência nova, sem mudança de IA ou de respostas do WhatsApp.
- Validação: **858 testes passando** em PostgreSQL local (809 no baseline, 49 novos); smoke da
  imagem Docker com Gunicorn 26.2.0/Python 3.12/Django 5.2.18 servindo `/health/` e `/ready/`
  (200 com banco local; 503 com banco inacessível).

### Fixed — auditoria estrutural final
- Normalização de imagens remove explicitamente metadados herdados, inclusive o perfil ICC
  que o encoder PNG preservava implicitamente; orientação e SHA-256 dos bytes enviados mantidos.
- Evolução mensal histórica explicita a janela e o ano; listagem de receitas corrige
  concordância de gênero. Sem alteração de cálculos, autorização ou chamadas de IA.
- Documentada a limitação de reversão da migration `whatsapp/0005` com linhas NULL.
- Regressões para ICC na fronteira Vision, mensagens com expectativas literais e falha no
  commit PostgreSQL após persistir transação/anexo. Relatório em `docs/AUDIT-FINAL.md`.
- Validação final: **809 testes passando** em PostgreSQL local (800 no baseline, 9 novos);
  Ruff check/format, Django check, migrations check, pip check e git diff check aprovados.

### Added — gestão operacional pelo Django Admin
- UserAdmin com nome, username, telefone, status, privilégios e família/papel atuais; filtro
  com/sem família e consultas sem N+1. Cadastro sugere senha inutilizável; senha explícita
  continua disponível, sem exibição de hash. Telefone usa o normalizador existente antes de
  validar tamanho/unicidade, inclusive formatos brasileiros equivalentes.
- FamilyAdmin com ADMIN/membros visíveis e operações explícitas para ADMIN inicial, inclusão,
  remoção e transferência. Transferência atômica com lock da família, validação do destino e
  LogEntry no mesmo commit; inclusão/remoção compartilham o lock. FamilyMember somente leitura.
- Desativação mantém todo o histórico. Exclusão administrativa de User/Family bloqueada,
  evitando cascatas e perda de identidade; FKs e schema inalterados.
- ConversationContext registrado somente leitura; conteúdo de mensagens/transcrições oculto;
  ReceiptAttachment com metadados e seção LEGACY sem links. Ledger e catálogo mantêm proteções.
- Staff limitado não pode modificar outra conta staff nem seus próprios privilégios.
- Regressões de autorização, histórico, rollback, concorrência PostgreSQL, privacidade e N+1.
  Nenhuma migration, dependência nova, alteração de RLS ou integração Storage.
- Validação automatizada: **800 testes passando** em PostgreSQL (baseline 752; 48 novos),
  Ruff check/format, Django check, migrations check, pip check e git diff check aprovados.
- Smoke operacional local concluído com três usuários fictícios, família com dois MEMBERs,
  transferência, bloqueio de remoção do ADMIN, desativação e telas financeiras/anexos somente leitura;
  banco PostgreSQL descartável destruído ao final.

### Changed — comprovantes sem armazenamento
- Imagens de comprovante são processadas de forma efêmera, em memória, e **não são retidas**: download, validação,
  normalização (sem EXIF/GPS) e visão acontecem em memória; nada vai para Storage, disco, banco
  ou logs. `IMAGE_RETENTION = NONE`.
- `ReceiptAttachment` vira metadados técnicos gravados após a visão: `PROCESSED` no mesmo atomic
  da transação/mensagem, `FAILED` em falha de visão ou commit. `sha256` = bytes normalizados
  enviados à visão (auditoria, não deduplica).
- Migration `whatsapp/0005`: `storage_bucket`/`storage_path` anuláveis (legado, `NULL` em linhas
  novas), `CHECK` aceita `NULL`; nenhuma tabela nova, nenhum dado removido.
- Resposta de comprovante não diz mais "Comprovante guardado." (nada é guardado).
- Validação ao vivo concluída (2026-10-08): recibo real pelo WhatsApp DEV processado sem Storage
  e sem service role key. `IMAGE_NO_STORAGE_LIVE_VALIDATION = COMPLETE`.

### Removed
- `common/storage.py`, `storage_check`, testes do cliente Storage e as variáveis
  `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_STORAGE_BUCKET`,
  `SUPABASE_TIMEOUT_SECONDS` (a aplicação não precisa mais da service role key).

### Changed — respostas do WhatsApp
- Registro de transação (`CREATE_TRANSACTION`) responde com card estruturado (título, valor,
  categoria, subcategoria quando existir e data sempre visível); entradas passam a se chamar
  "Receita" nas respostas.
- Demais respostas (TOTAL, BALANCE, LIST, TOP, BREAKDOWN, COMPARE, UPDATE, UNDO, erros, família,
  membro, resumo, evolução mensal e variação por categoria) reescritas em tom conversacional:
  totais zerados viram "não teve gastos", saldo negativo é escrito por extenso e listas perdem a
  numeração. Valores, cálculos, autorização e selectors inalterados.
- O resumo do período (`PERIOD_SUMMARY`) passa a ser descrito em parágrafos com receitas,
  gastos, saldo, % da renda comprometida, categoria principal e maior gasto.

### Fixed — auditoria V1.3–V1.5
- Respostas da Evolution agora têm leitura limitada também fora do caminho feliz de mídia;
  corpos HTTP de erro são limitados a 4 KiB e demais respostas JSON a 1 MiB.
- `MONTHLY_TREND` normaliza a janela efetiva: `months` prevalece sobre `start_date`, `end_date`
  só ancora o mês final e o contexto não preserva uma data auxiliar de um único dia.
- `SUPABASE_TIMEOUT_SECONDS` precisa ser positivo e rejeições compartilhadas de mídia usam o
  evento de log correto para áudio e imagem.
- Timeout default do Gunicorn elevado de 60 s para 120 s, acima do pior caminho síncrono
  configurado para áudio/imagem.
- Falha de persistência do `ReceiptAttachment` após upload agora encerra a mensagem como falha e
  remove o objeto recém-enviado, evitando mensagens presas em `RECEIVED`.

### Added — V1.5: análises financeiras
- Validação ao vivo concluída (2026-10-07): 8 consultas reais pelo WhatsApp (SELF, FAMILY,
  FAMILY_MEMBER e follow-up), valores conferidos no banco, sem mutações. `V1_5_LIVE_VALIDATION = COMPLETE`.
- `apps/transactions/analytics.py`: `period_summary`, `monthly_trend`, `category_trend` sobre o
  `ReadScope` autorizado (SELF/FAMILY/FAMILY_MEMBER da V1.2), agregados no PostgreSQL, DTOs em
  `Decimal`, nada persistido.
- Contrato de IA `v1.5`: `query_kind` `PERIOD_SUMMARY`, `MONTHLY_TREND`, `CATEGORY_TREND` e
  campos `months`, `trend_metric`, `trend_direction` (validados no Django; guardados no contexto).
- Respostas locais curtas e neutras em `whatsapp/messages.py` (sem aconselhamento).
- `common.money.percent` (Decimal, 1 casa, ROUND_HALF_UP, `None` com denominador zero).
- Migration `transactions/0003`: índice `(family, transaction_date)` (aplicada no Supabase DEV).
- Testes: `test_analytics.py` e `test_pipeline_v15.py`.

### Changed
- `TOP` aceita `INCOME` ("maior entrada"); com 1 item a resposta é "Maior gasto/entrada ...".
- `COMPARE` passou a usar `common.money.percent` (mesmo resultado).
- Prompt `2026-10-07.4` compactado (descrições de `query_kind`/`scope` mais curtas).
- Limitação conhecida: `CATEGORY_TREND` padrão = mês corrente até hoje × mês anterior completo.

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
