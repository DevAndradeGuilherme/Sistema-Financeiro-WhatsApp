# Auditoria independente do hardening operacional de pré-produção

Data: 2026-10-09. Escopo restrito ao hardening operacional; a estrutura aprovada anteriormente
não foi reaberta.

## Escopo e evidências

- Baseline estrutural: `6edf1638d70331b5f81e9359670b258613716e96`.
- Commit auditado: `126cfd5d268924b9f3637cd9f4d165d5a44a0cfa`
  (`chore: harden operations for production`).
- Estado inicial: working tree limpa, `HEAD` no commit auditado, nenhum arquivo não rastreado e
  nenhuma migration no delta.
- Baseline reproduzido antes das correções: **858 testes passaram** em PostgreSQL local.
- Estado final após a auditoria: **873 testes passaram** em PostgreSQL local (15 regressões
  adicionadas pela auditoria).
- Também passaram: Ruff check/format, Django check, `makemigrations --check --dry-run`, `pip check`
  e `git diff --check`.
- O delta e seus call graphs foram revisados; buscas de segredos/dados reais não encontraram
  credenciais, documentos, telefones reais nem mídia. Ocorrências sensíveis em testes são
  exclusivamente valores sintéticos usados para provar ausência de vazamento.

## Achados e correções

CRITICAL: 0. HIGH: 0. MEDIUM: 2. LOW: 2. Todos os achados MEDIUM/LOW foram corrigidos.

| ID / nível | Localização e reprodução | Impacto / causa / por que os testes não pegaram | Menor correção segura |
|---|---|---|---|
| M1 MEDIUM | `apps/whatsapp/recovery.py::diagnose`; mensagem com `instance` de 100 e ID externo de 128 caracteres, seguida de UPDATE ou UNDO | A chave calculada chega a 238 caracteres, enquanto `TransactionChange.origin` persiste os primeiros 200. A busca pela chave inteira produzia falso negativo: efeito existente podia aparecer como ausente e liberar `mark-failed` em estado anômalo. Os testes usavam identificadores curtos. | Consultar a forma exata persistida, obtendo o limite do próprio campo; regressões para UPDATE e UNDO com origem longa. |
| M2 MEDIUM | `apps/whatsapp/client.py::EvolutionClient.send_text`; HTTP 2xx com `{}`, `{"key": {}}` ou ID vazio | O envio era tratado como sucesso sem confirmação do provider; `resend-reply` podia gravar `REPLY_RESENT` embora a Evolution não tivesse aceitado uma mensagem identificável. Havia testes para timeout, conexão, 4xx/5xx e non-JSON, mas não para JSON semanticamente inválido. | Exigir `key.id` string não vazia e lançar `EvolutionResponseError`; recovery mantém `REPLY_SEND_FAILED`. Testes das cinco classes de falha. |
| L1 LOW | `inbound_recovery list --limit -1` | O slicing negativo do QuerySet emitia traceback (`ValueError`). Não havia mutação, mas a interface operacional não recusava o argumento de forma limpa e limites enormes podiam causar carga acidental. Testes só cobriam o valor padrão. | Aceitar apenas 1–1000 e retornar `CommandError`; regressões para 0, -1 e 1001. |
| L2 LOW | `inbound_recovery resend-reply <id> --no-reply --execute` | A flag incompatível era silenciosamente ignorada e a operação enviava o card. O nome da operação ainda era explícito e não havia risco financeiro, mas a interface surpreendia o operador. Não havia teste de combinação de flags. | Recusar `--no-reply` em `list`, `inspect` e `resend-reply`; a flag fica exclusiva de `mark-failed`. |

INFO:

- `REPLY_RESEND_PENDING` não é status do model; é código operacional em `error_code`. A escolha
  evita migration e preserva os cinco estados persistidos.
- Um timeout depois de a Evolution aceitar a mensagem mantém a possibilidade de card duplicado.
  Não existe duplicação financeira e exactly-once de entrega exigiria infraestrutura fora da V1.
- Gunicorn 26.2.0 existe no PyPI, requer Python >=3.10, declara suporte a Python 3.12 e não possui
  dependência runtime obrigatória. A série 26 mantém WSGI e gerenciamento graceful. As versões
  26.2.1/26.2.2 posteriores corrigem principalmente ASGI/HTTP2, recursos não usados pelo projeto;
  não foi feito upgrade além do escopo. Fontes: [PyPI 26.2.0](https://pypi.org/project/gunicorn/26.2.0/),
  [releases upstream](https://github.com/benoitc/gunicorn/releases) e
  [settings upstream](https://github.com/benoitc/gunicorn/blob/master/docs/content/reference/settings.md).

## State machine real

Estados persistidos de `InboundMessage`: `RECEIVED`, `READY`, `IGNORED`, `PROCESSED` e `FAILED`.

```text
reserva -> RECEIVED
RECEIVED -> IGNORED                         classificação/filtro terminal
RECEIVED -> READY                           texto ou promoção condicional do áudio
RECEIVED -> PROCESSED                       imagem ou mídia rejeitada respondida
RECEIVED -> FAILED                          falha técnica ou mark-failed seguro
READY    -> PROCESSED                       efeito/consulta e status no mesmo atomic
READY    -> FAILED                          falha técnica sem efeito ou mark-failed seguro

PROCESSED + REPLY_SEND_FAILED
  -> REPLY_RESEND_PENDING                   claim commitado antes do HTTP
  -> REPLY_RESENT                           envio confirmado
  -> REPLY_SEND_FAILED                      envio falhou de forma conhecida
```

As três últimas marcas são `error_code`, não novos estados. `IGNORED` e `FAILED` não voltam ao
pipeline. Não existe transição administrativa que edite ledger ou reative mensagem.

## Recovery e efeitos financeiros

O comando não chama interpretação, OpenAI, transcrição, Vision, planner, `commit_plan`, analytics
nem services CREATE/UPDATE/UNDO. O call graph permitido termina em diagnóstico, transição
condicional para `FAILED`, reconstrução local do card por `created_reply` e `send_reply`.

CREATE é detectado por `Transaction.idempotency_key`, usando o manager que inclui soft-deleted.
UPDATE e UNDO são detectados por `TransactionChange.origin`, agora na forma realmente persistida.
`ReceiptAttachment` presente em mensagem não terminal também força recusa conservadora.

`mark-failed` aceita somente RECEIVED/READY antigos, sem evidência de efeito. Ele trava a linha com
`select_for_update`, refaz diagnóstico e idade sob `transaction.atomic()` e usa transição
condicional. `commit_plan` trava a mesma linha antes de qualquer efeito; promoção de áudio também
é condicional. Assim, ou o worker vence e conclui/atualiza a atividade, ou o recovery vence e o
worker atrasado não grava efeito.

`resend-reply` exige PROCESSED + `REPLY_SEND_FAILED`, intenção CREATE, transação vinculada com a
mesma chave, ativa, com exatamente o CREATE no ledger, usuário ativo e telefone presente. O card
usa somente `Transaction`/categoria persistidas. UPDATE, UNDO, consultas, analytics e respostas de
erro/rejeição são recusados. O lock protege a reivindicação, que é commitada como
`REPLY_RESEND_PENDING`; a chamada HTTP ocorre fora da transação. Duas execuções concorrentes
autorizam no máximo um envio.

Se o processo morrer depois do claim e antes/durante o HTTP, a mensagem permanece
`REPLY_RESEND_PENDING`: fica visível em `list`/`inspect`, é recusada nas execuções seguintes e exige
registro de incidente. Não há loop nem caminho para recriar efeito. Timeout após entrega pode gerar
um segundo card, jamais uma segunda transação.

## Interface, privacidade e endpoints

Dry-run é o padrão; `--execute` é obrigatório para mutação. IDs inexistentes, operação inválida,
idade abaixo de cinco minutos, `--execute` em leitura e `--limit` fora de 1–1000 falham sem efeito.
A saída contém somente IDs internos, referência externa parcial, códigos, timestamps, IDs técnicos,
telefone mascarado e indicadores booleanos; não mostra texto/transcrição, valores, mídia, telefone
completo, headers ou segredos.

`/health/` executa somente código local e continua 200 sem banco. `/ready/` executa `SELECT 1`; o
connect é limitado por `DB_CONNECT_TIMEOUT` e o statement por 2 s. `set_config(..., true)` roda
dentro de `transaction.atomic()`, portanto o valor é local e é descartado no commit/rollback. Um
teste força cancelamento PostgreSQL, confirma conexão sem rollback pendente e executa novo probe
200 na mesma sequência. Falhas retornam corpo 503 fixo e logam só a classe da exceção.

Somente as expressões exatas `^health/$` e `^ready/$` estão em `SECURE_REDIRECT_EXEMPT`; as demais
rotas mantêm redirect HTTPS. A exceção atende probes internos e não amplia bypass por prefixo.

## Gunicorn, packaging e Docker

O pin mudou somente de 23.0.0 para 26.2.0. `--no-control-socket` existe desde 25.1 e desativa a
interface `gunicornc`, não os sinais normais de graceful shutdown/reload. O projeto usa o worker
`sync`, WSGI Django 5.2 e Python 3.12, combinação suportada.

Build limpo da imagem passou. Smoke Linux confirmou Gunicorn 26.2.0, dois workers sync e ausência
de `packaging` no runtime; `/health/` retornou 200 e `/ready/` retornou 503 com banco propositalmente
inacessível. Gunicorn não declara dependências obrigatórias; `packaging` permanece no ambiente dev
por ser dependência do pytest.

## Retenção e runbook

`OPERATIONS.md` registra: Transaction enquanto a conta existir; TransactionChange junto ao ledger;
ReceiptAttachment metadata junto ao histórico; `IMAGE_RETENTION = NONE`; InboundMessage inicialmente
preservada por auditoria/idempotência; ConversationContext elegível a expurgo futuro; logs com meta
de 30 dias; retenção dos providers tratada separadamente. Nenhum expurgo foi implementado.

O runbook é executável após longo intervalo: contém `list`, `inspect`, dry-run, `--execute`,
`mark-failed`, resend, casos de recusa, proibição de SQL manual, registro de incidente e relação
entre min-age e `GUNICORN_TIMEOUT`.

## Qualidade dos testes e concorrência

Os testes de concorrência usam `pytest.mark.django_db(transaction=True)`, duas threads, conexões
Django thread-local separadas, barreira simultânea e PostgreSQL real. Eles verificam uma única ação,
um único envio e estado final; não são execuções sequenciais mascaradas por `TestCase`.

Os testes de recovery também falham se interpretação, transcrição, Vision ou services financeiros
forem chamados; verificam ledger inalterado, soft delete, usuário inativo, transação alterada,
dry-run, worker atrasado e saída redigida. Readiness derruba cursor e executa timeout real no
PostgreSQL. As regressões dirigidas de CREATE, UPDATE, UNDO, texto, áudio, imagem, idempotência,
usuário inativo, falha de resposta e autorização familiar estão incluídas na suíte completa.

## Limitações A–F

| Limitação | Classificação | Decisão |
|---|---|---|
| A. Respostas de rejeição/erro não são recuperadas | ACCEPTABLE_FOR_INITIAL_PROD | Sem efeito financeiro; contato manual atende o volume inicial. |
| B. Query/update/undo não são reconstruídas | ACCEPTABLE_FOR_INITIAL_PROD | Reconstruir poderia responder com estado atual diferente; recusa é segura. |
| C. Timeout do envio original pode duplicar card | ACCEPTABLE_FOR_INITIAL_PROD | Efeito financeiro continua exactly-once; risco está documentado. |
| D. Worker atrasado pode mandar segunda mensagem técnica | ACCEPTABLE_FOR_INITIAL_PROD | O fence impede qualquer efeito; duplicação é apenas informativa. |
| E. Expurgo ainda futuro | POST_PROD | Política inicial existe; medir volume antes de implementar. |
| F. min-age precisa acompanhar `GUNICORN_TIMEOUT` | SHOULD_FIX_BEFORE_PROD | No default, 15 min > 120 s. O setup deve registrar/revalidar essa relação se mudar timeout. |

## Riscos e decisão

BLOCKERS: nenhum.

SHOULD_FIX_BEFORE_PROD: durante infraestrutura, manter min-age acima da janela máxima de worker;
configurar/testar Nginx/TLS, host/domínio, segredos, webhook, backup/restauração e rotação de logs;
dimensionar workers/memória/timeouts/rate limit; concluir a validação real de MEMBER já pendente.

ACCEPTABLE_FOR_INITIAL_PROD: limitações A–D, para cerca de 15 usuários, com operação manual e
registro de incidente conforme o runbook.

POST_PROD: expurgo de InboundMessage/ConversationContext baseado em volume real e revisão periódica
das retenções dos providers.

Após as correções e checks finais:

```text
OPERATIONAL_HARDENING_APPROVED = YES
SAFE_TO_TAG_RC2 = YES
READY_FOR_INFRASTRUCTURE_SETUP = YES
```

Nenhum push, tag ou deploy foi realizado.
