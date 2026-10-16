# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).
Versões seguem o `ROADMAP.md`. Toda mudança de comportamento, contrato (IA, webhook,
schema) ou decisão arquitetural deve gerar entrada em `Unreleased`.

## [Unreleased]

### Fixed — auditoria final do patch de mutações (4 × P1)
- P1#1: datas do texto ("dia 5", "outubro", "outubro de 2026", dd/mm, hoje/ontem/
  anteontem, dia da semana, esse mês/semana/ano, mês/semana/ano passado) agora são resolvidas
  em intervalos (`hints.temporal`) e o alvo proposto pela IA precisa caber nelas; antes bastava
  o texto "ter alguma data" ("apague a entrada do dia 5" podia apagar a do dia 7).
  Expressão não resolvível → `TARGET_UNCLEAR`.
- P1#2: UPDATE só seleciona por "último" ou critério além do tipo comprovado pelo texto como
  alvo; valor/data/categoria/subcategoria/descrição inventados pela IA (ou o próprio valor novo)
  não escolhem mais a transação. "Último" + filtro não comprovado → `TARGET_UNCLEAR` (vale
  também para UNDO).
- P1#3: `InboundMessage` passa a ser atribuída ao remetente cadastrado na própria reserva
  (mesmo `INSERT`, com lock da linha do usuário); uma mensagem reservada e ainda não
  classificada não fica mais invisível para a sequência de "sim"/correção. Autorização
  inalterada (classificação continua decidindo; desconhecido sem `user`; fromMe/grupo fora).
- P1#4: "80"/"ok, 80" após um CREATE/UPDATE recente cujo vínculo foi interrompido (ajuda,
  consulta, falha, mídia inválida) não cria lançamento (`CORRECTION_NOT_CREATED`); TTL continua
  sendo o limite.

### Fixed — riscos residuais de mutação (guards determinísticos)
- Ordem explícita de remoção ("remova", "apague", "exclua", "delete", "desfaça", "cancele o
  lançamento") classificada como `CREATE` pela IA criava um lançamento: agora nada é criado
  (`TARGET_UNCLEAR`). Verbo explícito de edição + `CREATE` → `CORRECTION_NOT_CREATED`.
- Formas coloquiais "tira", "retira", "estorna" (e infinitivos) aplicadas a um lançamento
  ("tira esse gasto de 30", "estorna o gasto de 50") entram no mesmo guard; sem o objeto
  financeiro ("tira-gosto", "tirar uma tatuagem", "retirei 200 no caixa") nada muda.
- "Desfaça a entrada"/"apague o gasto" removiam o único candidato do tipo nos 90 dias: tipo
  sozinho não identifica alvo → zero mutação e "Qual entrada/gasto você quer remover?".
- "O gasto de ontem era 40": a IA podia usar "ontem" só como data nova e editar o lançamento do
  contexto; o dia ligado ao lançamento agora precisa estar nos critérios, senão `TARGET_UNCLEAR`.

### Fixed — auditoria independente do patch de UPDATE/UNDO (REJECTED → corrigido)
- "desfazer", "remove", "apaga" (e "cancela" sem pendência) sozinhos executavam
  `undo_last_change`: agora não identificam nada → zero mutação + pedido de identificação, mesmo
  com `last=true` (ou tipo/descrição inventados) pela IA. A pilha só com "desfaz a última
  alteração/ação". "Último" exige o marcador no texto.
- Descrição era descartada quando havia categoria/subcategoria; agora todos os critérios em
  AND. `hints.py` lê também números (pt-BR), datas explícitas, itens do catálogo e membros; em
  UPDATE e UNDO, critério citado e omitido/contradito pela IA → `TARGET_UNCLEAR`; o alvo
  precisa de um critério comprovado pelo texto.
- Nome de outro membro da família no texto bloqueia UPDATE/UNDO (`FORBIDDEN_OTHER_ACCOUNT`)
  mesmo que a IA o omita ("apague a entrada de 77 da Maria" não apaga a da própria Alice).
- Seleção do alvo acontecia antes do lock por usuário: agora `commit_plan` trava mensagem →
  remetente (`lock_user`) e só então seleciona (`FOR UPDATE`), valida 0/1/N e executa no mesmo
  atomic.
- `PendingAction` (migration `whatsapp/0007`): vínculo com a mensagem do pedido, critérios
  aprovados e impressão SHA-256 dos candidatos; confirmação recusa qualquer alteração (categoria,
  descrição, data, troca de valores mantendo o total, soft delete). Só a mensagem seguinte ao
  pedido pode confirmá-lo: qualquer mensagem intermediária (inclusive falha de IA, áudio/imagem
  inválidos, ajuda) a invalida.
- Correção contextual ligada à sequência de mensagens: só o efeito da mensagem imediatamente
  anterior (ainda sendo a última alteração daquela transação); "não 100" confere o valor atual.
- "Desculpa, foram 80", "Não, foram 80", "Foi 80, não 100" e mensagens só com valor logo após
  um registro não criam lançamento (`CORRECTION_NOT_CREATED`).
- Prompt `PROMPT_VERSION 2026-10-15.3`: exemplos de `UNDO` sem "desfazer = última ação".
- `transactions.services.lock_user` público; `find_transactions(transaction_ids=, for_update=)`;
  `families.selectors.other_member_names` inclui o username de quem não tem primeiro nome.

### Fixed — integridade de UPDATE/UNDO e correções contextuais (beta fechada)
- "Desfaça a entrada de 50", "desfaça a entrada do dia 07/10" e "desfaça todas as entradas"
  removiam o lançamento mais recente (um gasto de R$ 34): `UNDO_TRANSACTION` ignorava qualquer
  critério (`UndoPlan` sem referência → `undo_last_change`) e o prompt mandava `reference` só
  em `UPDATE`. Agora `UNDO` com critérios remove exatamente o lançamento descrito (soft delete,
  `DELETE` com `origin`), aplicando tipo, valor, `transaction_date`/período, categoria e
  descrição nas transações do remetente: 0 → "não encontrei", 2+ → lista e pergunta.
- "Último" só com marcador explícito no texto ("último gasto" = EXPENSE, "última entrada" =
  INCOME); `UPDATE` sem critérios não cai mais no último lançamento.
- "Errado, recebi somente 1.050,41" criava uma receita: correções (`correction` da IA ou
  marcador no texto) nunca criam; sem critérios, editam só o lançamento recém-criado/editado
  (TTL, sem consulta depois), senão perguntam "Qual lançamento você quer corrigir?".
- Exclusão em massa exige confirmação: `PendingAction` (nova tabela, migration
  `whatsapp/0006`) com os ids exatos, 10 min, "sim"/"não" sem chamada à IA; qualquer outra
  mensagem cancela; conjunto alterado → nada removido. Edição em massa é recusada.
- Defesa em profundidade: `whatsapp/hints.py` lê tipo/valor/data/"todos"/"último"/correção no
  texto; critério citado mas omitido pela IA → pergunta, nunca busca mais ampla.
- Contrato `v1.5.2` (`PROMPT_VERSION 2026-10-15.2`): `reference` também em `UNDO`, com
  `start_date`/`end_date` e `all`; novo `correction` (boolean).
- Domínio: `find_transactions` com período e `since` opcional; `summarize_own_transactions`
  (soma no PostgreSQL); `last_change`; `soft_delete_transaction(origin=)` com lock do usuário e
  `soft_delete_transactions` (tudo ou nada, só o dono).

### Fixed — dono da transação ao citar membro da família (beta fechada)
- "Curso da Ayumi, 300 reais" era recusado como se fosse um lançamento para a Ayumi: o prompt
  mandava preencher `target_member_name` para qualquer pessoa citada e `actions.plan` recusava
  todo `CREATE` com nome. Agora `CREATE` pertence sempre ao remetente; o nome fica na descrição.
- Contrato `v1.5.1` (`PROMPT_VERSION 2026-10-15.1`): novo campo `for_another_person`
  (boolean, só `CREATE`) separa pedido explícito de lançar para outra pessoa de mera menção;
  em `CREATE`, `scope`/`target_member_name` são ignorados.
- Defesa em profundidade: o Django recusa `CREATE` quando o texto lança explicitamente para outro
  membro da família ("registra… para X", "conta da X", "X gastou…"), mesmo que a IA não sinalize.
- Resposta `OWN_ACCOUNT_ONLY` orienta a reenviar como "paguei…" ou pedir que a pessoa registre.
- Consultas (ADMIN sobre membro/família) e isolamento de MEMBER inalterados. Sem migration.

### Fixed — auditoria independente do hardening operacional
- Recovery passa a detectar UPDATE/UNDO mesmo quando a chave de origem ocupa o limite persistido
  do campo de auditoria, evitando falso negativo de efeito financeiro.
- Envio pela Evolution exige `key.id` na resposta de sucesso; resposta JSON incompleta permanece
  diagnosticável como falha de envio em vez de encerrar o recovery como reenviado.
- `inbound_recovery --limit` aceita somente 1–1000 e `--no-reply` é recusado fora de
  `mark-failed`; combinações inválidas falham com erro operacional limpo.
- Auditoria e decisão RC2 documentadas em `AUDIT-PREPROD-1.md`; sem migration.

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
