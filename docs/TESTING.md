# Testes

## 1. Ferramentas

- `pytest` + `pytest-django` (settings `config.settings.test`, definido no `pyproject.toml`).
- PostgreSQL real em testes. SQLite é proibido: constraints, `NUMERIC`, `ArrayField` e
  índices parciais precisam se comportar como em produção.
- `ruff` para lint e formatação (configuração no `pyproject.toml`).
- Sem rede em testes padrão: OpenAI e Evolution sempre mockados.

## 1.1 Como rodar

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # Linux/macOS: .venv/bin/
docker compose --profile test up -d test-db     # PostgreSQL 17 descartável (tmpfs) em 127.0.0.1:5433
pytest                                           # suíte completa
ruff check . && ruff format --check .            # lint
```

## 1.2 Banco de testes — regras de segurança

- O test runner do Django **cria e destrói** um banco (`test_<NAME>`). Por isso a suíte nunca
  usa o Supabase nem as variáveis `DB_*` da aplicação.
- `config/settings/test.py` lê apenas `TEST_DB_*` (padrão: container local acima) e **recusa**:
  `DJANGO_ENV=production`, qualquer host contendo `supabase` e o mesmo host/porta de `DB_*`.
- `DJANGO_SECRET_KEY` de teste é um valor fixo inseguro, usado só por essas settings.
- Para usar outro PostgreSQL local/CI, defina `TEST_DB_HOST`, `TEST_DB_PORT`, `TEST_DB_USER`,
  `TEST_DB_PASSWORD`, `TEST_DB_NAME`.

## 2. Organização

```
apps/<app>/tests/      testes de unidade e de serviço do app
common/tests/          helpers (dinheiro, datas, telefone) e infraestrutura (RLS, health/readiness)
tests/                 integração ponta a ponta (webhook → resposta) — a partir do webhook
tests/fixtures/        payloads Evolution anonimizados, respostas de IA fixas
```

- Nome: `test_<comportamento>_<condição>` (ex.: `test_member_cannot_view_other_member_transactions`).
- Fábricas simples (funções ou `factory_boy` se justificado). Sem fixtures globais mágicas.
- Fixtures compartilhadas em `conftest.py` na raiz (`make_user`, `alice`, `bob`, categorias,
  `make_transaction`).
- Datas: usar datas fixas ou relativas a `common.dates.today_local()`; para "agora", usar
  `unittest.mock.patch("django.utils.timezone.now", ...)`. Testes não podem depender do dia
  em que rodam.

## 3. O que testar obrigatoriamente

### Dinheiro
- `amount` sempre `Decimal`; conversão de string; rejeição de negativo, zero, NaN, excesso
  de casas, acima do limite.
- Agregações retornam `Decimal` exato (ex.: `0.10 + 0.20 == 0.30`).

### Datas
- "ontem", "sexta-feira", "dia 2" resolvidos a partir de `reference_date` fixa.
- Data futura rejeitada. Virada de dia em `America/Sao_Paulo` vs UTC
  (ex.: 23:30 local = dia seguinte em UTC).

### Autorização (matriz completa de `DOMAIN.md §6`)
- MEMBER não consulta outro membro; ADMIN consulta; ADMIN **não** edita/desfaz de terceiro;
  usuários de famílias diferentes isolados; usuário sem família isolado.
- Saída da IA com `scope: FAMILY`/`target_user` de outro usuário enviada por MEMBER → negado.

### Gestão operacional no Django Admin

- `accounts/tests/test_admin_operations.py`: cadastro sem senha ou com senha explícita, telefone
  canônico e duplicatas equivalentes em add/change, ativação/desativação preservando histórico,
  bloqueio de inativos antes dos pipelines de texto/áudio/imagem, exclusão bloqueada, proteção
  contra autopromoção/tomada de outra conta staff e consultas sem N+1.
- `families/tests/test_admin_operations.py`: ADMIN inicial obrigatório, inclusão somente MEMBER,
  duplicata/outra família negadas, remoção preserva snapshot, transferência restrita à família,
  rollback durante promoção e falha do LogEntry, GET sem mutação, CSRF, matriz de permissões e
  duas transferências concorrentes em conexões PostgreSQL separadas (mesmo destino ou destinos
  diferentes). A unique parcial existente permanece como proteção adicional.
- Regressões V1.2 continuam cobrindo tentativas de zero/dois ADMINs e rebaixamento isolado,
  adaptadas à interface explícita que substituiu o inline editável.
- `whatsapp/tests/test_admin_privacy.py`: contexto/mensagem/anexo somente leitura, transcrições
  ocultas e referências legadas sem links, incluindo anexos novos com `storage_* = NULL`.
- Smoke operacional local: login de superuser DEV, cadastro de três usuários fictícios,
  criação de família, inclusão de dois MEMBERs, transferência, remoção do ADMIN negada,
  desativação e páginas financeiras/anexos somente leitura. Usar banco PostgreSQL local
  descartável e limpar os dados ao final; nunca inserir dados financeiros reais.

### Categorias
- Somente slugs ativos aceitos; `kind` compatível; subcategoria pertence à categoria;
  fallback aplicado; IA não cria categoria.

### Idempotência
- Mesmo webhook entregue 2x → 1 `InboundMessage`, 1 `Transaction`, 1 resposta.
- Constraint de `idempotency_key` impede duplicata mesmo chamando o service direto 2x.

### Webhook
- Rede real bloqueada em toda a suíte (fixture autouse em `conftest.py`); Evolution
  sempre mockada (`unittest.mock` em `apps.whatsapp.client.urlopen`).
- Payloads de referência da Evolution 2.3.7 em `apps/whatsapp/tests/payloads.py`.
- Concorrência de idempotência: duas threads com conexões próprias (`transaction=True`).
- Sem segredo / segredo errado → rejeitado sem efeitos.
- `fromMe`, grupo, número desconhecido → ignorados sem chamar IA.
- Payload malformado → `400`, sem exceção não tratada.

### Contrato de IA e pipeline
- `apps/ai/tests`: schema estrito, parse de saídas válidas/maliciosas, mapeamento de erros do
  SDK (timeout, 401/403/404/429/5xx, incompleta, refusal, JSON inválido), parâmetros da chamada
  (`store=False`, modelo da config, uma chamada, sem retries), logs sem conteúdo.
- `apps/whatsapp/tests/test_pipeline.py`: `interpret_message` mockado com a proposta esperada
  para cada frase pt-BR; valida criação, fallback, datas, consultas, isolamento, idempotência,
  falha de envio sem recriação e rollback atômico.
- V1.1: `apps/transactions/tests/test_changes.py` (update/undo/auditoria/concorrência por threads,
  comparação, busca de referência) e `apps/whatsapp/tests/test_pipeline_v11.py` (edição,
  desfazer, follow-ups, TTL, isolamento, top/breakdown/lista/comparação, atomicidade).
  Fixtures do pipeline em `apps/whatsapp/tests/conftest.py`.
- V1.2: `apps/transactions/tests/test_scopes.py` (autorização de escopo, resolução de nomes,
  ambiguidade, snapshot histórico, isolamento entre famílias, ranking em 1 query),
  `apps/whatsapp/tests/test_pipeline_v12.py` (ADMIN/MEMBER ponta a ponta, mutações em
  terceiros, contexto familiar, perda de papel) e `apps/families/tests/test_admin_v12.py`.
- V1.3: `common/tests/test_media.py`, `apps/ai/tests/test_transcription.py`,
  `apps/whatsapp/tests/test_client_audio.py` (download com teto, base64, sem fetch de URL do
  payload) e `apps/whatsapp/tests/test_audio.py` (áudio → pipeline real: CREATE/INCOME/QUERY/
  UPDATE/UNDO, família, contexto texto↔áudio, filtros antes do download, idempotência,
  concorrência, falhas, limites, privacidade). Download/transcrição mockados na fronteira.
- V1.4: `common/tests/test_images.py` (formatos, falsificação, corrupção, limites, bomba,
  animação, EXIF/GPS, ICC e preservação da transparência), `apps/ai/tests/test_vision.py` e `apps/whatsapp/tests/test_image.py`
  (pipeline real com Pillow e domínio; Evolution e visão mockados nos clients; qualquer outro
  acesso de rede — ex. Storage — falha o teste; `storage_path` NULL, sem `SUPABASE_*`/service
  role, hash dos bytes normalizados, sem bytes/base64 no banco, idempotência, concorrência,
  falhas de visão/commit/anexo, privacidade). A regressão de commit injeta erro na chamada
  de commit após os INSERTs e verifica rollback do ledger/anexo e reentrega sem novo efeito.
- V1.5: `apps/transactions/tests/test_analytics.py` (resumo, evolução mensal, variação por
  categoria, percentuais `Decimal`, receitas = 0, saldo negativo, >100%, soft delete, edição,
  escopos FAMILY/FAMILY_MEMBER, agregação no banco) e `apps/whatsapp/tests/test_pipeline_v15.py`
  (PERIOD_SUMMARY/MONTHLY_TREND/CATEGORY_TREND ponta a ponta: SELF, ADMIN família/membro, MEMBER
  negado, membro externo, contexto manipulado, perda de papel, follow-ups, `months` padrão/3/12/
  acima do máximo, precedência de `months` sobre `start_date` e janela efetiva no contexto,
  1 chamada de IA, nenhuma mutação, falha de envio sem recálculo, janela histórica com ano
  verificada por expectativa literal independente do helper de formatação).
- Validação ao vivo V1.5 (2026-10-07): 8 mensagens reais pelo WhatsApp do ADMIN DEV; para cada uma,
  `InboundMessage` PROCESSED, 1 chamada OpenAI, 1 envio, contagens de `Transaction`/`TransactionChange`
  inalteradas e valores conferidos por SQL somente leitura no Supabase DEV (incluindo uma despesa
  fictícia soft-deleted criada via services, que não entrou em nenhum total).
- Validação integrada local V1.5 (manual, fora da suíte): pipeline real (`handle_ready_message`)
  + OpenAI real + Supabase DEV com usuários/família/lançamentos sintéticos, Evolution
  substituída por captura em memória e tudo dentro de um `transaction.atomic()` revertido ao
  final (nada persiste, nada é enviado). Não substitui a validação ao vivo pelo WhatsApp.
- Smokes de imagem: comprovantes sintéticos gerados em memória (sem dado pessoal); nunca
  versionar imagens. Não há mais smoke de Storage.
- Smoke de transcrição: fixture sintética gerada por TTS fora do repositório (nunca versionar
  áudio, muito menos voz pessoal).
- Smoke test real (manual, fora da suíte): chamar `apps.ai.service.interpret_message` com o
  contexto real, sem persistir, e conferir intenção/valor/categoria/datas e tokens.

- Integridade de UPDATE/UNDO (beta, 2026-10-15): `apps/whatsapp/tests/test_mutation_targets.py`
  (remoção por tipo/valor/`transaction_date`, 0/1/N candidatos, "último" explícito, critérios
  omitidos pela IA, correção contextual × CREATE, confirmação em massa com expiração/cancelamento/
  substituição/conjunto alterado, MEMBER/ADMIN, idempotência, áudio e a sequência real da beta),
  `apps/whatsapp/tests/test_hints.py` (leitura do texto e "sim"/"não") e
  `apps/transactions/tests/test_targets.py` (filtros/período, soma no banco, soft delete em lote
  tudo-ou-nada, `last_change`).
- Guards de intenção (2026-10-15): `apps/whatsapp/tests/test_mutation_intent_guards.py`
  (remoção/edição explícita proposta como CREATE, tipo sozinho em UNDO, data de referência em
  UPDATE usada só como valor novo, CREATE normal preservado).
- Auditoria independente (2026-10-15): `apps/whatsapp/tests/test_audit_mutations.py` (os 43
  casos: "desfazer"/"remove"/"apaga"/"cancela" sem efeito, `last` malicioso, AND de categoria +
  descrição + valor, critérios omitidos/contraditos em UPDATE/UNDO, nome de membro no texto,
  impressão da `PendingAction` — categoria/descrição/data/troca de valores/soft delete —,
  invalidação por qualquer mensagem intermediária — IA fora, ajuda, não entendida, áudio e
  imagem inválidos —, correção ligada à mensagem anterior, frases corretivas e elípticas,
  idempotência) e `apps/whatsapp/tests/test_mutation_concurrency.py` (PostgreSQL real, threads:
  confirmações simultâneas, confirmação × edição, dois UPDATE/DELETE do mesmo alvo
  interpretados antes de qualquer commit, dois pedidos em massa, confirmação duplicada).
- Auditoria final (2026-10-16): `apps/whatsapp/tests/test_final_audit_p1.py` (datas do texto
  comparadas por valor — dia N, mês, mês+ano, dd/mm, hoje/ontem/anteontem, não resolvíveis —;
  filtros de UPDATE inventados pela IA — valor, data, período, categoria, subcategoria,
  descrição antiga ou igual à nova, "último" + filtro extra —; "80"/"ok, 80" após mutação
  recente interrompida por ajuda, desconhecida, consulta, falha de IA, áudio e imagem
  inválidos) e `apps/whatsapp/tests/test_reserve_ordering_concurrency.py` (PostgreSQL real,
  threads: mensagem reservada e pausada entre reserva e classificação não fica invisível para
  "sim" nem para a correção "80"; remetente desconhecido e eco fromMe não entram na ordem).

### Soft delete
- Transação desfeita não aparece em consultas nem totais.

### Operação (health/readiness e recuperação manual)
- `common/tests/test_health.py`: liveness sem acesso ao banco (teste sem `django_db`) e de pé
  com o banco quebrado; readiness 200 com PostgreSQL, 503 com `OperationalError`/`InterfaceError`/
  `DatabaseError` sem vazar host/usuário/senha na resposta ou no log, 503 quando o
  `statement_timeout` cancela a consulta (PostgreSQL real), só `SELECT`s e nenhuma chamada a
  Evolution/OpenAI/httpx; POST negado.
- `apps/whatsapp/tests/test_recovery.py`: diagnóstico de cada estado (travada, em curso, efeito
  sem status terminal, efeito por `TransactionChange.origin`, anexo em imagem travada, FAILED com/
  sem efeito, intenções não deriváveis, transação alterada, usuário inativo, reenvio interrompido);
  dry-run sem mutação; `mark-failed` sem reprocessar (interpretação/transcrição/visão/services
  financeiros falham o teste se chamados), idempotente e com cerca contra worker de texto/áudio
  atrasado; `resend-reply` ponta a ponta após falha real de envio no pipeline, sem nova chamada de
  IA nem alteração do ledger, no máximo uma vez, falha mantém recuperável; concorrência com duas
  conexões PostgreSQL (um único envio/transição); saída do comando sem telefone completo, texto,
  valores ou id completo do provedor; recusa com erro e sem efeito.

## 4. Testes com serviços reais

Chamadas reais à OpenAI/Evolution apenas em testes marcados (ex.: `@pytest.mark.live`),
desativados por padrão, executados manualmente com credenciais de desenvolvimento.
Nunca rodar contra o banco de produção.

## 5. Critério mínimo para merge

- Todos os testes passam; lint sem erros.
- Mudança em autorização, dinheiro, datas ou idempotência acompanha teste novo
  que falharia sem a mudança.
- Bug corrigido acompanha teste de regressão.
