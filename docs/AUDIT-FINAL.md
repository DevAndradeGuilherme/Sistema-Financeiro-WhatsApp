# Auditoria estrutural final

Data: 2026-10-08. Revisão independente da implementação do Admin.

## Escopo e baseline

Somente deltas posteriores à Auditoria #2 (`32d27c4`): mensagens (`f387798`),
comprovantes efêmeros (`5fcae6d`), evidência documental ao vivo (`8236a55`) e
Admin operacional (`631b0a6`). Working tree inicialmente limpa; commits confirmados.
Documentos obrigatórios lidos, incluindo contratos de domínio, segurança e testes.
Sem deploy, produção, novas funcionalidades, dependências ou redesenho.

Antes de alterar arquivos: **800 testes passaram** em PostgreSQL local (261,27 s).
Ruff check/format, Django check, makemigrations dry-run, pip check e git diff check
passaram. Comandos Django usaram `--settings=config.settings.test` para isolar o
banco; nenhuma migration foi aplicada ao Supabase durante esta auditoria.

## Achados comprovados e correções

CRITICAL: 0. HIGH: 0. MEDIUM: 1. LOW: 3. Todos os achados abaixo corrigidos.

| ID / nível | Localização e reprodução | Impacto / causa / lacuna dos testes | Menor correção aplicada |
|---|---|---|---|
| F1 MEDIUM | `common/images.py::_encode`; PNG sintético com `icc_profile` mantém esse campo após normalização e na entrada da Vision | Metadados incorporados podem ser enviados ao provedor externo, contrariando o contrato. Pillow herda ICC de `image.info` sem argumento explícito. Testes cobriam EXIF/GPS em JPEG e texto em PNG, não ICC. Falha herdada do normalizador, encontrada na regressão dirigida de privacidade do no-Storage. | Remover ICC de `image.info` depois da orientação, preservando transparência; regressões JPEG/PNG/WEBP e pipeline PNG. |
| F2 LOW | `apps/whatsapp/messages.py::monthly_trend`; consultar novembro/dezembro de 2025 em 2026 | Resposta dizia “últimos 2 meses” e omitia o ano, inconsistência herdada e mantida na reescrita. Cabeçalho não considerava a âncora histórica; testes só usavam janelas correntes e helper que repetia a regra de ano. | Passar a data de referência já disponível; explicitar janela histórica e ano. Teste de pipeline com datas e resposta literais. |
| F3 LOW | `apps/whatsapp/messages.py::listing`; LIST INCOME com/sem período | “Estes são suas últimos receitas” / “Estes foram suas receitas”. Demonstrativo/adjetivo masculinos fixos; casos existentes não verificavam esses cabeçalhos. | Concordância feminina; duas expectativas literais. |
| F4 LOW | `docs/DATABASE.md`, migration `whatsapp/0005`; reversão restaura NOT NULL sobre linhas novas NULL | Rollback operacional inviável sem tratar dados; documentação omitia a limitação. Testes de schema atual não exercitavam rollback histórico. | Documentar limitação e manter schema expandido/runtime compatível; sem inventar paths, apagar dados ou mudar migration. |

Na reprodução inicial, cinco de oito casos falharam antes das correções; três já passavam
(formatos não afetados e rollback de commit). O nono teste protege a transparência PNG.

## Confirmações da investigação

- **Mensagens:** CREATE EXPENSE/INCOME, TOTAL, BALANCE, LIST, TOP, BREAKDOWN, COMPARE,
  UPDATE, UNDO, FAMILY/FAMILY_MEMBER, PERIOD_SUMMARY, MONTHLY_TREND, CATEGORY_TREND e
  erros continuam locais. Selectors/analytics e cálculos não mudaram; dinheiro segue
  Decimal. Sem resposta final da OpenAI nem segunda chamada. Campos do card conferem;
  subcategoria ausente é omitida; zero, saldo negativo, receita zero e previous=0 tratados.
  O resumo reduz os campos exibidos conforme o delta documentado, sem mudar agregações.
- **Imagem:** reserva idempotente → Evolution por id → memória → validação/Pillow →
  orientação e remoção de metadados → hash → uma Vision (`store=False`, retries=0) →
  plan_receipt → commit atômico → descarte. Nenhum upload, URL Storage, arquivo temporário,
  imagem/base64 persistidos ou service role necessária. Somente bytes normalizados vão à Vision.
- **ReceiptAttachment:** novos `storage_bucket`/`storage_path` NULL; tipo, tamanho, SHA-256,
  dimensões, documento, status e transação coerentes. Hash confere com bytes da Vision.
  Legado fica somente como referência DEV, sem link, recuperação ou preview pelo runtime.
- **Falhas/idempotência:** download/normalização/Vision/output inválido/plano rejeitado,
  escrita de anexo/efeito e envio preservam ausência de efeito parcial. Teste adicional
  injeta falha na chamada de commit, depois dos INSERTs, e verifica rollback de Transaction,
  TransactionChange e anexo de sucesso; resta diagnóstico FAILED, sem repetição na reentrega.
  Mesmo id reserva uma vez (inclusive concorrência); nova mensagem com mesmo hash é válida.
- **UserAdmin:** cadastro com senha explícita ou inutilizável, normalização compartilhada,
  telefone duplicado canônico rejeitado, busca/filtros e senha sem hash na tela. Staff limitado
  não promove a si mesmo nem toma outra conta staff/superuser, inclusive endpoint de senha.
  Desativação preserva memberships/ledger/auditoria/mensagens; inativos barrados antes dos
  pipelines texto/áudio/imagem. Exclusão individual e action de exclusão não são permitidas.
- **Famílias:** criação exige ADMIN inicial; inclusão é MEMBER; remoção preserva snapshot.
  Transferência valida destino e ADMIN atual, trava família com FOR NO KEY UPDATE, rebaixa,
  promove e revalida no atomic. Destino externo/inexistente/já ADMIN e família sem ADMIN
  são recusados. Transferências concorrentes para mesmo/diferentes destinos deixam exatamente
  um ADMIN; repetição imediata para quem já é ADMIN é recusada. Falha na promoção ou LogEntry
  reverte operação. Inclusão/remoção compartilham lock; FamilyMemberAdmin é somente leitura.
- **Permissões:** views protegidas por admin_view e CSRF, GET sem mutação, POST exige permissões
  da família e da operação de membership. Staff sem ambas recebe 403 no endpoint direto;
  alvo sempre filtrado pela família. Family ADMIN/MEMBER não recebem privilégios Django.
- **Ledger/Admin:** Transaction, TransactionChange, InboundMessage, ConversationContext e
  ReceiptAttachment sem add/change/delete; endpoints diretos testados, sem action financeira.
  Exclusão de User/Family bloqueada. Catálogo mantém escrita exclusiva de superuser e constraints
  de fallback. LogEntry registra criação, membros e transferência no mesmo atomic; edição de
  is_active usa logging/transação padrão do Django. Eventos de transferência contêm ids, não
  senha, telefone ou valores. Transcrições ocultas; anexos sem mídia; acesso financeiro depende
  das permissões Django de visualização, não de papel familiar.
- **Queries:** UserAdmin usa select_related; FamilyAdmin count/prefetch; FamilyMemberAdmin
  select_related. Sem N+1 evidente nas colunas dessas listagens.
- **Schema/RLS/dependências:** 0005 é a única migration do conjunto; SQL revisado, sem DML,
  DROP de coluna/tabela ou perda de legado. Nenhuma migration do Admin nem dependência nova.
  Documentação consistente com RLS 19/19 e zero policy pública previamente verificados em DEV;
  não é uma nova medição remota. Nenhuma mudança de RLS necessária para Admin/0005.
- **Storage/segredos:** ocorrências restantes são histórico documental, comentários, campos/
  migrations legados e testes negativos; nenhum cliente/comando/config de Storage no runtime.
  Varredura por tokens de provedores, JWT, chave privada, URL com credencial e atribuições de
  segredos em 850 arquivos de cinco snapshots (working tree versionada + quatro commits):
  apenas fixtures/config de testes. `.env` ignorado, não versionado; nenhum untracked inicial.
  Nenhum valor sensível local foi impresso. Varredura por padrões não prova ausência universal.

## Testes e limites da evidência

Os 48 casos adicionados pelo Admin verificam banco, endpoints, permissões, rollback e
concorrência PostgreSQL; não dependem apenas de botões ocultos. Mocks de transporte/IA ficam
nas fronteiras. Helpers de respostas mensais que espelham formatação não foram considerados
prova independente: a regressão histórica nova usa texto/datas/valores literais.
O teste antigo chamado “failure while committing” falhava em save, antes do commit;
a lacuna crítica agora tem teste próprio de falha de commit (sem mudança no pipeline).
Isso não simula perda de conexão com resultado de commit desconhecido.

Adicionados **9 casos**: ICC (3 formatos), PNG→Vision (1), transparência PNG (1), receitas (2),
janela histórica (1), commit/idempotência (1). Validação final: **809 testes passaram**
em PostgreSQL local (118,92 s; `TEST_DB_HOST=127.0.0.1`, mesmo container descartável).
Ruff check/format, Django check, `makemigrations --check --dry-run`, `pip check` e
`git diff --check` passaram. Sem migration nova. Regressão dirigida intermediária:
133 testes de imagem/mensagens/analytics passaram; nenhuma integração externa real executada.

## Pendências pré-produção e riscos aceitos

| Classificação | Itens restantes |
|---|---|
| BLOCKER para deploy | Provisionar/configurar VPS, domínio, Nginx/TLS, estáticos/Admin, segredos, webhook e procedimento de deploy/rollback. Validar backup e restauração para o plano Supabase escolhido. Nenhum blocker estrutural de código remanescente. |
| SHOULD_FIX_BEFORE_PROD | Definir prazos, acesso e expurgo de InboundMessage (texto/transcripts), ReceiptAttachment metadata e logs; preservar chaves de idempotência e vínculos/auditoria ao planejar expurgo. Documentar recuperação manual e observabilidade de mensagens interrompidas; dimensionar memória/workers/timeouts/rate limit. Revalidar e tratar a pendência de suporte do Gunicorn 23 herdada da Auditoria #2, sem upgrade nesta revisão. Conferir/limpar objetos DEV antigos antes de encerrar a retenção legada. |
| ACCEPTABLE_FOR_INITIAL_PROD | Processamento síncrono para até aproximadamente 15 usuários após dimensionamento; recuperação manual, sem worker/retry automático; comparação de mês parcial com anterior completo; MEMBER live pendente como risco operacional aceito com rollout monitorado e matriz automatizada. |
| POST_PROD | Automatizar recuperação/expurgo conforme política e métricas; reavaliar capacidade/índices; remover colunas Storage legadas em migration própria após limpeza. |

`IMAGE_RETENTION = NONE` no backend auditado; não implica política de retenção dos provedores
externos. `store=False` é parâmetro da chamada, não evidência de descarte em toda a cadeia.
Na preparação, conferir retenção/configuração da Evolution e do provedor de IA. Nada foi
apagado do Storage DEV nesta auditoria. Não há limpeza automática de texto/metadados/logs;
a política desses dados deve ser decidida antes do primeiro uso real.

`V1_5_LIVE_VALIDATION = COMPLETE` e `IMAGE_NO_STORAGE_LIVE_VALIDATION = COMPLETE` mantidos
como evidências anteriores, sem nova execução ao vivo. `MEMBER_LIVE_VALIDATION =
PENDING_LIVE_VALIDATION` permanece pendência operacional, não bug de código.

## Decisão

`STRUCTURAL_DEVELOPMENT_COMPLETE = YES`

`READY_FOR_PRODUCTION_PREPARATION = YES`

A preparação pode começar; estes estados não autorizam nem significam deploy concluído.
