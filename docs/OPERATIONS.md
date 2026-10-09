# Operação

Runbook de pré-produção: health/readiness, recuperação manual de mensagens e retenção de dados.
Estados das mensagens: [`WHATSAPP.md §5`](WHATSAPP.md). Deploy: [`DEPLOYMENT.md`](DEPLOYMENT.md).
Segurança: [`SECURITY.md`](SECURITY.md).

## 1. Health e readiness

| Endpoint | Tipo | Verifica | Resposta |
|---|---|---|---|
| `GET /health/` | liveness | só que o processo Django responde; não toca banco nem serviços externos | `200 {"status": "ok"}` |
| `GET /ready/` | readiness | `SELECT 1` no PostgreSQL com `statement_timeout` local de 2 s (conexão limitada por `DB_CONNECT_TIMEOUT`) | `200 {"status": "ready"}` ou `503 {"status": "unavailable"}` |

- Nenhum dos dois consulta OpenAI ou Evolution, grava dados ou revela versão, host, usuário,
  exceção ou configuração. Falha de readiness é registrada só com o tipo do erro
  (`readiness.failed check=database error=OperationalError`).
- Use `/health/` para reiniciar o container (o `HEALTHCHECK` do Dockerfile usa este) e `/ready/`
  para decidir se o proxy/orquestrador deve enviar tráfego. Banco fora do ar não deve reiniciar
  o processo em loop; deve só tirá-lo do balanceamento.
- Ambos ficam isentos do redirect HTTPS em produção (`SECURE_REDIRECT_EXEMPT`), para checagens
  locais por HTTP; o `Host` usado precisa estar em `DJANGO_ALLOWED_HOSTS` (ex.: `127.0.0.1`).

## 2. Recuperação manual de mensagens

O processamento é síncrono e **não há retry automático**. Uma mensagem pode ficar interrompida
quando o worker morre no meio da requisição (timeout do Gunicorn, deploy, OOM) ou quando o envio
da resposta falha depois do efeito. A recuperação é **manual, por mensagem e explícita**.

### 2.1 Princípios

- Recuperação **nunca reprocessa** a mensagem: interpretação, transcrição, visão e services
  financeiros não são executados de novo. A mensagem pode ser antiga ("ontem" viraria outro dia)
  e o usuário pode já ter reenviado o pedido, o que a idempotência não detecta (outro id).
- O ledger (`Transaction`, `TransactionChange`, `ReceiptAttachment`) nunca é alterado.
  Valor, categoria, data, família, escopo e transação não são editáveis por este caminho.
- Dry-run é o padrão; mutação exige `--execute`. Toda verificação é refeita sob
  `SELECT ... FOR UPDATE` da mensagem — o mesmo lock que o commit do efeito usa —, então duas
  execuções simultâneas (ou um worker atrasado) agem no máximo uma vez.
- Estado que não pode ser provado é **recusado**. Não adivinhe.

### 2.2 Comando

```bash
python manage.py inbound_recovery list [--limit 50] [--min-age-minutes 15]
python manage.py inbound_recovery inspect <id>
python manage.py inbound_recovery resend-reply <id> [--execute]
python manage.py inbound_recovery mark-failed <id> [--execute] [--no-reply] [--min-age-minutes 15]
```

`<id>` é o ID interno de `InboundMessage` (coluna `id` do `list`/Django Admin). A saída mostra
apenas: id, `provider_ref` parcial (`3EB0…0042`), tipo, status, códigos, intenção, timestamps,
`user_id`, telefone mascarado (`+55319****0001`), existência de efeito financeiro e ação segura.
Nunca mostra texto, transcrição, valores, imagem/base64, telefone completo ou segredos.
Recusas terminam com erro (`CommandError`, código de saída ≠ 0) e não alteram nada.

`list` mostra RECEIVED/READY/FAILED e mensagens com `REPLY_SEND_FAILED`/`REPLY_RESEND_PENDING`,
mais recentes primeiro. FAILED aparece para diagnóstico; em geral não exige ação.

### 2.3 Diagnóstico e ações

Efeito financeiro = existe `Transaction` com `idempotency_key = whatsapp:<instance>:<id>`
(inclusive soft-deleted) **ou** `TransactionChange` com `origin` igual a essa chave.

| Estado encontrado | Efeito | `safe_action` (`reason`) | O que fazer |
|---|---|---|---|
| RECEIVED/READY sem atividade há < 15 min | não | `WAIT` (`MAY_BE_IN_FLIGHT`) | Aguardar; pode estar em processamento |
| RECEIVED/READY sem atividade há ≥ 15 min | não | `MARK_FAILED` (`STUCK_WITHOUT_EFFECT`) | `mark-failed` (falha **antes** do efeito) |
| RECEIVED/READY | sim, ou anexo gravado | `REFUSE` (`EFFECT_WITHOUT_TERMINAL_STATUS`) | Incidente: viola o invariante efeito+status |
| PROCESSED + `REPLY_SEND_FAILED`, CREATE, transação desta mensagem ativa e sem alterações | sim | `RESEND_REPLY` (`REPLY_DERIVABLE_FROM_LEDGER`) | `resend-reply` (falha **depois** do efeito) |
| PROCESSED + `REPLY_SEND_FAILED`, consulta/edição/desfazer | conforme intenção | `REFUSE` (`REPLY_NOT_DERIVABLE`) | Sem ação pelo comando; avisar o usuário manualmente se necessário |
| PROCESSED + `REPLY_SEND_FAILED`, CREATE já editada/desfeita | sim | `REFUSE` (`TRANSACTION_CHANGED_SINCE`) | Sem ação; o card original não representa mais o estado |
| PROCESSED + `REPLY_SEND_FAILED`, CREATE sem transação vinculada desta mensagem | ? | `REFUSE` (`EFFECT_NOT_CONFIRMED`) | Incidente |
| PROCESSED + `REPLY_SEND_FAILED`, usuário inativo/sem telefone | sim | `REFUSE` (`RECIPIENT_UNAVAILABLE`) | Sem envio |
| PROCESSED + `REPLY_RESEND_PENDING` | sim | `REFUSE` (`RESEND_OUTCOME_UNKNOWN`) | Reenvio interrompido; entrega desconhecida. Não repetir |
| PROCESSED (demais, inclusive `REPLY_RESENT`) | conforme intenção | `NONE` (`PROCESSED`) | Nada |
| FAILED / IGNORED sem efeito | não | `NONE` | Nada (usuário já recebeu o erro técnico, quando aplicável) |
| FAILED / IGNORED com efeito | sim | `REFUSE` (`EFFECT_ON_NON_PROCESSED`) | Incidente |

**`mark-failed`** marca `FAILED` com `error_code = STUCK_RECEIVED`/`STUCK_READY` usando a mesma
transição condicional do pipeline (`services.mark_failed`, só a partir de RECEIVED/READY) e envia
a resposta já existente de erro técnico ("Não consegui processar sua mensagem agora. Tente de novo
em alguns instantes."), salvo `--no-reply` ou usuário inativo/sem telefone. Um worker atrasado que
ainda tenha a mensagem em memória encontra `FAILED` sob o lock e não grava efeito (texto, áudio e
imagem verificam o status antes do commit/promoção); no pior caso ele envia uma segunda resposta
de erro técnico, sem efeito financeiro. `--min-age-minutes` não aceita menos de 5
(acima do timeout padrão do Gunicorn, 120 s); se `GUNICORN_TIMEOUT` for aumentado, use uma idade
maior que ele.

**`resend-reply`** reconstrói o card de registro a partir da `Transaction` persistida com a mesma
função do pipeline (`actions.created_reply`) e o envia pela Evolution. Sequência: lock → revalida →
`error_code = REPLY_RESEND_PENDING` (commit) → envio fora da transação → `REPLY_RESENT` (sucesso) ou
volta a `REPLY_SEND_FAILED` (falha, pode tentar de novo). Se o processo morrer durante o envio,
fica `REPLY_RESEND_PENDING` e passa a ser recusado. Como um timeout no envio original pode ter
entregado a mensagem, o usuário pode receber o card duas vezes — nunca há segundo efeito financeiro.

Limitações conhecidas: falhas de envio de respostas de rejeição/erro (`PROCESSED` com outro
`error_code`, `FAILED`) não são registradas pelo pipeline e não são reenviadas; respostas de
consultas, edições e desfazer não são reconstruídas.

### 2.4 Procedimento

1. Listar: `python manage.py inbound_recovery list`.
2. Para cada linha com `MARK_FAILED`, `RESEND_REPLY` ou `REFUSE`:
   `python manage.py inbound_recovery inspect 128`.
3. Confirmar efeito financeiro pelos campos `transaction_created`/`ledger_changes` do `inspect`
   (ou, no Django Admin somente leitura, pela transação vinculada à mensagem). Não é necessário SQL.
4. Rodar a ação em dry-run (`resend-reply 128` ou `mark-failed 131`), ler a saída e só então
   repetir com `--execute`.
5. `REFUSE` → não executar nada; abrir incidente (§2.6).

Exemplo fictício:

```text
$ python manage.py inbound_recovery inspect 131
id: 131
provider_ref: 3EB0…9F21
type: AUDIO
status: RECEIVED
error_code: -
...
phone: +55319****0001
transaction_created: não
ledger_changes: não
receipt_status: -
safe_action: MARK_FAILED
reason: STUCK_WITHOUT_EFFECT

$ python manage.py inbound_recovery mark-failed 131
... DRY-RUN: nada foi alterado. mark-failed é seguro; repita com --execute.
$ python manage.py inbound_recovery mark-failed 131 --execute
... EXECUTADO: mark-failed; resposta enviada.
```

### 2.5 O que não fazer

- Não usar SQL manual (`UPDATE`/`DELETE`/`INSERT`) como procedimento normal de recuperação, nem
  para "destravar" status, nem para corrigir valores. Correções financeiras são feitas pelo
  próprio usuário (editar/desfazer) ou por processo administrativo documentado.
- Não reenviar o webhook à aplicação nem pedir reentrega à Evolution: a reserva idempotente trata
  como duplicata e nada acontece.
- Não executar ações em lote; não usar `--min-age-minutes` para "furar" a janela de execução.
- Não colar texto de mensagens, telefones completos ou valores em tickets/chats.

### 2.6 Registro de incidente

Para todo `REFUSE` inesperado ou recuperação executada, registrar (em canal privado da operação):
data/hora (America/Sao_Paulo), ID interno, `provider_ref` parcial, status/`reason` antes e depois,
ação executada (ou "nenhuma"), quem executou e a causa provável (ex.: restart durante deploy,
timeout). Sem texto, valores, telefone completo ou segredos. Incidentes que indiquem violação de
invariante (efeito sem status terminal) devem ser investigados antes de qualquer nova ação.

## 3. Retenção de dados (política inicial)

Política inicial para o primeiro uso real; **nenhum expurgo automático está implementado**.
Qualquer exclusão futura deve preservar chaves de idempotência e vínculos de auditoria.

| Dado | Retenção |
|---|---|
| `Transaction` | Enquanto a conta existir (soft delete não remove a linha) |
| `TransactionChange` | Junto ao ledger (auditoria e "desfazer") |
| `ReceiptAttachment` | Somente metadados técnicos, retidos junto ao histórico |
| Imagens de comprovante | `IMAGE_RETENTION = NONE` — processadas em memória e descartadas |
| Áudio | Não retido; só a transcrição entra em `InboundMessage.text` |
| `InboundMessage` | Preservada inicialmente (auditoria e idempotência); prazo/expurgo de `text` a definir com volume real |
| `ConversationContext` | Dado operacional (TTL 20 min); elegível a limpeza futura após inatividade |
| Logs de aplicação/containers | Objetivo de 30 dias em produção (rotação do host/driver de logs) |

A retenção dos provedores externos (OpenAI, Evolution API/WhatsApp, Supabase, host) é **separada**
e segue a configuração/contrato de cada um. `store=False` na OpenAI é um parâmetro da chamada,
não garantia de retenção zero; a Evolution guarda mensagens no próprio banco
(`DATABASE_SAVE_DATA_NEW_MESSAGE=true`, necessário para o download de mídia). Revisar essas
configurações na preparação de produção.
