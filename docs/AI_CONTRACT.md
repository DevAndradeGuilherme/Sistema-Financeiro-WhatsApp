# Contrato de IA

Como o app `ai` usa a OpenAI e o que o Django faz com a resposta.
Regras de negócio aplicadas na validação: [`DOMAIN.md`](DOMAIN.md). Pipeline: [`WHATSAPP.md §5`](WHATSAPP.md).

## 1. Papel da IA

A IA **interpreta** uma mensagem em linguagem natural e devolve uma **proposta estruturada**.
A proposta é entrada não confiável: o Django valida, resolve referências e autoriza.

A IA nunca: acessa banco, gera/executa SQL, calcula totais, decide permissões, escolhe o
usuário consultado, cria categorias/usuários ou tem efeito colateral. Não há tools nem
function calling; a única saída é o objeto estruturado. A resposta ao usuário é montada pelo
Django (`apps/whatsapp/messages.py`).

## 2. Integração (implementada)

- SDK oficial `openai` (versão fixada em `requirements.txt`), **Responses API**
  (`client.responses.create`), **Structured Outputs** com JSON Schema `strict: true`.
- **Uma chamada por mensagem**; `max_retries=0` no SDK (sem chamadas extras ocultas).
- `store=False`; sem Conversations, `previous_response_id`, threads ou histórico remoto.
- Modelo: `OPENAI_MODEL` (env). Timeout: `OPENAI_TIMEOUT_SECONDS`. Opcional:
  `OPENAI_REASONING_EFFORT` (`none|low|medium|high`), enviado só quando definido, pois nem
  todo modelo aceita. Sem fallback silencioso para outro modelo.
- `max_output_tokens = 1024` (o objeto usa ~60–110).
- Validado ao vivo com `gpt-6-luna` + `OPENAI_REASONING_EFFORT=none`. Contrato v1.0: ~860 tokens
  de entrada, ~60–70 de saída. Contrato v1.1: ~1.260 de entrada (+~400: regras de
  edição/desfazer/novas consultas e schema maior com objetos aninhados), ~85–145 de saída
  (objetos `reference`/`updates`), +~60 de entrada em follow-ups (consulta anterior).
  0 tokens de raciocínio; 1,5–4 s por mensagem.
  Contrato v1.2: ~1.420 de entrada (+~160: regras de `scope`) tanto em SELF quanto em FAMILY,
  ~100–150 de saída. **Nenhuma lista de membros é enviada** (custo zero e sem dados pessoais):
  o modelo só copia o nome escrito; o Django resolve dentro da família.

### Contexto conversacional (V1.1)

Para follow-ups, a última consulta **validada** do remetente (ids, datas, tipo, `limit`, e na
V1.2 `scope` + nome do membro já resolvido — sem
valores, textos ou dados de terceiros) é enviada como mensagem `developer` separada
(`Previous query: {...}`), antes da mensagem do usuário. As instruções não mudam (prefixo
estável). Contexto vem de `ConversationContext` (por usuário, TTL
`CONVERSATION_CONTEXT_TTL_MINUTES`, padrão 20 min) e nunca concede acesso: a consulta roda
sempre para o remetente. Mutações não usam contexto da IA: "muda para 15" e "errado, foram 80"
são resolvidos no banco (efeito da mensagem imediatamente anterior do remetente + TTL);
"desfazer" sozinho não identifica nada e não altera nada.
- Interface (`apps/ai/service.py`), sem acesso a models de domínio:

```python
def interpret_message(text: str, context: InterpretationContext) -> InterpretationResult
```

`InterpretationContext` contém apenas: `reference_date` (hoje em `America/Sao_Paulo`),
`timezone` e o catálogo **ativo** (id inteiro, nome, tipo, aliases; subcategorias ativas com
id, nome, aliases). **Não vão para a IA:** telefone, nome, username, ids de usuário, família,
outras transações, totais, saldos, papéis, segredos.

Prompt: `apps/ai/prompts.py` (`PROMPT_VERSION`), instruções curtas em inglês; texto do usuário
vai em `input`, separado das instruções. Prompt injection é tratado arquiteturalmente: o pior
resultado possível é uma proposta inválida, rejeitada pela validação.

## 3. Intenções

| Intenção | Versão | Descrição |
|---|---|---|
| `CREATE_TRANSACTION` | V1.0 ✅ | Registrar despesa ou receita |
| `QUERY_TRANSACTIONS` | V1.0/V1.1 ✅ | `query_kind`: `TOTAL`, `BALANCE`, `TOP` (maiores gastos), `BREAKDOWN` (por categoria), `LIST` (listagem curta), `COMPARE` (período A × B) |
| `UPDATE_TRANSACTION` | V1.1 ✅ | Corrigir lançamento próprio: `reference` + `updates` |
| `UNDO_TRANSACTION` | V1.1 ✅ | Remover o lançamento descrito em `reference`, ou desfazer a última ação quando o texto diz "última alteração" (v1.5.2; sem alvo seguro → nada muda; política em `DOMAIN.md §5`) |
| `HELP` | V1.0 ✅ | Ajuda (resposta local fixa) |
| `UNKNOWN` | V1.0 ✅ | Não identificado, fora do escopo ou sobre dados/lançamentos de outra pessoa |
| (família) | V1.2 ✅ | Consultas com `scope` `FAMILY`/`FAMILY_MEMBER` e `query_kind` `RANKING` (totais por pessoa) |
| (análises) | V1.5 ✅ | `query_kind` `PERIOD_SUMMARY` (resumo, "% da renda gasta"), `MONTHLY_TREND` (mês a mês), `CATEGORY_TREND` (variação por categoria); `TOP` aceita `INCOME` ("maior entrada") |

`COMPARE_PERIODS`, `TOP_EXPENSES` e `CATEGORY_BREAKDOWN` foram modelados como `query_kind`
(mesma intenção de consulta) em vez de intenções separadas.

O enum do schema contém apenas as intenções da versão corrente.

## 4. Schema de saída (`contract_version v1.5.2`)

Todos os campos presentes; ausência = `null` (requisito do modo estrito). `additionalProperties: false`.

| Campo | Tipo | Notas |
|---|---|---|
| `intent` | enum | Seção 3 |
| `transaction_type` | `EXPENSE` \| `INCOME` \| null | |
| `amount` | string \| null | Decimal com ponto: `"10.50"`. **Nunca número JSON.** |
| `description` | string \| null | Trecho curto ("gasolina") |
| `category_id` | integer \| null | id do catálogo fornecido |
| `subcategory_id` | integer \| null | id do catálogo fornecido |
| `transaction_date` | string \| null | `YYYY-MM-DD`, só se mencionada |
| `query_kind` | `TOTAL` \| `BALANCE` \| `TOP` \| `BREAKDOWN` \| `LIST` \| `COMPARE` \| `RANKING` \| `PERIOD_SUMMARY` \| `MONTHLY_TREND` \| `CATEGORY_TREND` \| null | |
| `start_date`, `end_date` | string \| null | `YYYY-MM-DD`, intervalo fechado (período A em `COMPARE`) |
| `compare_start_date`, `compare_end_date` | string \| null | período B em `COMPARE` |
| `limit` | integer \| null | só se o usuário pedir ("5 maiores"); Django aplica padrão 5, máximo 10 |
| `follow_up` | boolean | mensagem depende da consulta anterior ("e mês passado?") |
| `reference` | objeto \| null | `UPDATE`/`UNDO` (v1.5.2): `last` (bool, só com "último" no texto), `transaction_type`, `category_id`, `subcategory_id`, `description`, `amount`, `transaction_date`, `start_date`/`end_date` (período de `transaction_date`, v1.5.2), `all` (bool: vários de propósito, "todas as entradas", v1.5.2). Só **descreve** o alvo; nenhum critério pode ser omitido |
| `updates` | objeto \| null | `UPDATE`: `amount`, `description`, `category_id`, `subcategory_id`, `transaction_date` (null = não muda) |

| `scope` | `SELF` \| `FAMILY` \| `FAMILY_MEMBER` \| null | V1.2: de quem é o dinheiro citado (semântica; null = SELF) |
| `target_member_name` | string \| null | V1.2: nome como escrito ("Maria"); resolvido pelo Django dentro da família. Em `CREATE` deve ser null e é **ignorado** |
| `correction` | boolean | v1.5.2: a mensagem corrige algo dito antes ("errado, foram 80", "na verdade foi 950"). O Django **nunca cria** a partir dela; com `UPDATE` sem critérios, edita só o contexto inequívoco (§5, item 9) |
| `for_another_person` | boolean | v1.5.1, só `CREATE`: `true` = pedido explícito para lançar para outra pessoa ("registra 100 para a Maria", "na conta do João", "Maria gastou 100"); `false` quando o remetente pagou, mesmo citando alguém ("curso da Maria", "presente pra Maria") — o nome fica na `description`. Só pode **recusar**, nunca define dono |
| `months` | integer \| null | V1.5 `MONTHLY_TREND`: meses pedidos; Django aplica padrão 6, mínimo 1, máximo 12 |
| `trend_metric` | `EXPENSE` \| `INCOME` \| `BALANCE` \| null | V1.5 `MONTHLY_TREND` (null → `transaction_type` → `EXPENSE`) |
| `trend_direction` | `INCREASE` \| `DECREASE` \| `ALL` \| null | V1.5 `CATEGORY_TREND` (null → `ALL`) |

Objetos aninhados também são estritos (`additionalProperties: false`, todos os campos
obrigatórios). Não existem `transaction_id`, `target_user_id`, `target_phone`, `user_id`,
`role`, `permission`, `authorized`, `is_admin`, `confidence` nem tipo editável. `scope` e
`target_member_name` **não autorizam nada**: o Django decide (`DOMAIN.md §6`). `UPDATE`/`UNDO`
citando outra pessoa são recusados; mesmo sem essa pista, os services só operam sobre o remetente.
`CREATE` pertence **sempre** ao remetente: nome citado é beneficiário/descrição e não muda o dono;
pedido explícito de lançar para outra pessoa é recusado (ver §5, item 2).

```jsonc
// "10 reais de gasolina" (hoje = 2026-10-06)
{"intent":"CREATE_TRANSACTION","transaction_type":"EXPENSE","amount":"10.00",
 "description":"gasolina","category_id":2,"subcategory_id":1,"transaction_date":null,
 "query_kind":null,"start_date":null,"end_date":null}

// "Desfaça a entrada de 50 do dia 07/10" (v1.5.2; demais campos null/false)
{"intent":"UNDO_TRANSACTION","reference":{"last":false,"transaction_type":"INCOME",
 "category_id":null,"subcategory_id":null,"description":null,"amount":"50.00",
 "transaction_date":"2026-10-07","start_date":null,"end_date":null,"all":false}}

// "quanto gastei de transporte esse mês?"
{"intent":"QUERY_TRANSACTIONS","transaction_type":"EXPENSE","amount":null,
 "description":null,"category_id":2,"subcategory_id":null,"transaction_date":null,
 "query_kind":"TOTAL","start_date":"2026-10-01","end_date":"2026-10-06"}
```

Mudanças no schema são mudanças de contrato: atualizar este documento, `CONTRACT_VERSION`,
testes de contrato e `CHANGELOG.md`.

## 5. Validação no Django (`apps/whatsapp/actions.py`)

1. Forma: `apps/ai/contract.parse_output` (tipos, enums, campos exatos) → senão `INVALID_OUTPUT`.
2. Usuário: sempre o remetente autenticado (`InboundMessage.user`). Nenhum campo da IA o altera.
   `CREATE` ignora `scope`/`target_member_name`. É recusado (`FORBIDDEN_OTHER_ACCOUNT`, resposta
   `OWN_ACCOUNT_ONLY`, nada é gravado) se `for_another_person=true` **ou** se a verificação
   determinística do Django encontrar no texto, com o nome de outro membro atual da família do
   remetente, "registra/lança/anota… para X", "conta da X" ou "X gastou/recebeu/pagou…"
   (`actions._books_for_member`). Se ambos falharem, o pior caso é o lançamento ficar com o
   próprio remetente — nunca com outra pessoa.
3. `amount` → `Decimal` via `common.money.to_decimal` (sem float); faltando → pede o valor;
   inválido → resposta; `create_transaction` revalida (> 0, 2 casas, limite).
4. Categoria (criação): id precisa existir, estar ativo e aceitar o tipo; subcategoria precisa
   estar ativa, aceitar o tipo e pertencer à categoria (incompatível → descartada; sozinha →
   implica sua categoria). Sem categoria válida → **fallback explícito** do tipo
   (`Category.is_fallback`, ver `DOMAIN.md`).
5. Categoria (consulta): id inexistente/incompatível → resposta "não encontrei", nunca fallback.
6. Datas: ISO estrito; criação sem data → hoje; data futura rejeitada pelo service. Consulta sem
   período → mês corrente até hoje; só início → até hoje; só fim → o próprio dia; `start ≤ end`.
7. Totais, rankings, agrupamentos, listagens e comparações: somente
   `apps.transactions.selectors` (`get_total`, `get_period_totals`, `get_top_expenses`,
   `get_totals_by_category`, `list_transactions`, `compare_periods`). Percentual =
   `(A − B) / B × 100` em `Decimal`, 1 casa, `ROUND_HALF_UP`; com B = 0 não há percentual.
8. Padrões de consulta: `TOP` = gastos; `BREAKDOWN`/`COMPARE` sem tipo = gastos; `LIST` sem
   período = mais recentes; `COMPARE` sem período B = mês anterior completo ao início de A.
9. `UPDATE`/`UNDO` (v1.5.2): transação resolvida pelo Django nas transações ativas **do
   remetente** (`DOMAIN.md §5`), aplicando **todos** os critérios de `reference`; ids de
   `updates` revalidados; `update_transaction` reaplica todas as regras. Defesa em
   profundidade (`whatsapp/hints.py`, leitura determinística do texto):
   - todos os critérios combinados por AND (descrição também junto de categoria);
   - tipo citado no texto ("entrada"/"receita" × "gasto") é imposto; conflito com a IA → pergunta;
   - `UPDATE` e `UNDO`: valor, data explícita ou categoria/subcategoria do catálogo presentes no
     texto mas ausentes/contraditos na proposta (critério ou, em `UPDATE`, valor novo) → pergunta
     (`TARGET_UNCLEAR`), nunca uma busca mais ampla; critério de valor/data sem aparecer no
     texto também; o alvo precisa de ao menos um critério comprovado pelo texto;
   - nome de outro membro da família no texto → `FORBIDDEN_OTHER_ACCOUNT` em `UPDATE`/`UNDO`,
     mesmo com `scope=SELF` e sem `target_member_name`;
   - `last` só vale com "último"/"acabei de" no texto e nunca desempata critérios explícitos;
     "desfazer" sem nada identificável → `TARGET_UNCLEAR`, mesmo com `last=true` da IA; a pilha
     (`undo_last_change`) só com "desfaz a última alteração/ação";
   - 0 candidatos → `NOT_FOUND`; 2+ → `AMBIGUOUS_REFERENCE` com até 3 candidatos; nada muda;
   - vários de propósito (`all` ou "todas"/plural no texto) → `UNDO` pede confirmação do
     conjunto exato (`CONFIRMATION_REQUIRED`); `UPDATE` em massa é recusado;
   - `UPDATE` sem critérios ("errado, foram 80") → só o efeito da mensagem imediatamente
     anterior do remetente, com o valor novo presente no texto; senão `CORRECTION_TARGET_UNCLEAR`;
   - `CREATE` com `correction=true` **ou** marcador de correção no texto ("errado", "desculpa,
     foram", "não, foram", "foi X, não Y"...) → nada é criado (`CORRECTION_NOT_CREATED`); texto
     só com valor logo após um registro/edição também;
   - `CREATE` com verbo inequívoco de remoção no texto ("remova", "apague", "exclua", "delete",
     "desfaça", "cancele o lançamento") → nada é criado (`TARGET_UNCLEAR`); de edição ("altere",
     "edite", "mude o valor") → `CORRECTION_NOT_CREATED`;
   - `UNDO` individual com só o tipo ("desfaça a entrada") → `TARGET_UNCLEAR` ("Qual entrada?"),
     mesmo com um candidato; precisa de valor, data, item/descrição do texto ou "último";
   - `UPDATE` com dia ligado ao lançamento no texto ("o gasto de ontem era 40") exige esse dia
     nos critérios de `reference`; só como valor novo → `TARGET_UNCLEAR`.
   A seleção acontece na execução, sob o lock do remetente (`pipeline.commit_plan`).
   "sim"/"não" (mensagem inteira) **não vai para a IA**: responde a `PendingAction` do
   remetente (`whatsapp/pending.py`), só se for a mensagem seguinte ao pedido.
10. `follow_up=true` sem contexto válido → "Não consegui identificar a consulta anterior...".
11. V1.2: `scope`/`target_member_name` viram um `ReadScope` em `transactions.scopes` na execução
    (dentro do `atomic`): `FAMILY`/`FAMILY_MEMBER`/`RANKING` exigem ser o ADMIN atual; nome
    presente com `SELF` é tratado como `FAMILY_MEMBER`; `RANKING` é sempre `FAMILY`.
12. V1.5: análises usam `apps.transactions.analytics` com o mesmo `ReadScope` (item 11).
    Em `MONTHLY_TREND`, `months` define a largura da janela e prevalece sobre `start_date`;
    `end_date` apenas ancora o mês final (limitado a hoje). O contexto guarda a janela efetiva,
    não datas auxiliares incompatíveis devolvidas pelo modelo. `months` fora de 1..12 → padrão
    (≤ 0/null) ou limitado a 12 (resposta avisa);
    `CATEGORY_TREND` sem período B = mês anterior completo (mesma regra do `COMPARE`, ver
    limitação em `DOMAIN.md §4`); categoria inválida → "não encontrei". A IA só classifica: os
    valores e percentuais são calculados no banco/`Decimal` e o texto é montado localmente.
    O contexto de follow-up guarda também `months`, `trend_metric` e `trend_direction` validados.

## 6. Falhas

| Código (`InboundMessage.error_code = AI_<código>`) | Causa |
|---|---|
| `TIMEOUT`, `CONNECTION` | rede/timeout |
| `AUTH`, `PERMISSION` | chave inválida/sem acesso |
| `RATE_LIMIT` | 429 |
| `MODEL_UNAVAILABLE` | modelo inexistente/sem acesso (404) |
| `BAD_REQUEST` | ex.: modelo não suporta o schema/parâmetro |
| `PROVIDER_ERROR` | 5xx |
| `INCOMPLETE`, `REFUSAL`, `EMPTY_OUTPUT`, `INVALID_OUTPUT` | resposta inutilizável |

Todas: nenhuma transação, mensagem `FAILED`, resposta genérica ao usuário, sem retry.

## 7. Logs

`ai.interpret ref model prompt contract outcome intent duration_ms input_tokens output_tokens`.
Nunca: API key, prompt, texto da mensagem, objeto estruturado.

## 7.1 Transcrição de áudio (V1.3)

- `apps/ai/transcription.transcribe_audio(bytes, filename, content_type)`: SDK oficial,
  `client.audio.transcriptions.create(model=OPENAI_TRANSCRIPTION_MODEL, language="pt",
  response_format="json")`, `max_retries=0`, timeout `OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS`.
  Sem Realtime, sem translation, sem prompt (testes reais não mostraram necessidade), sem catálogo
  nem dados pessoais. Modelo sempre da configuração; sem fallback para outro modelo.
- Por mensagem de áudio: **1 transcrição + 1 interpretação** (a mesma do texto). O transcript
  não é "limpo" por outro modelo.
- Erros mapeados com os mesmos códigos da interpretação (`TIMEOUT`, `AUTH`, `PERMISSION`,
  `RATE_LIMIT`, `BAD_REQUEST` p/ arquivo inválido, `MODEL_UNAVAILABLE`, `PROVIDER_ERROR`) +
  `EMPTY_AUDIO`, `EMPTY_TRANSCRIPT`, `INVALID_OUTPUT`.
- Logs: `ai.transcribe ref model outcome duration_ms bytes` + usage (`seconds` ou tokens). Nunca
  o transcript ou o áudio.
- Validado ao vivo com `gpt-transcribe`: OGG/Opus aceito diretamente; uso reportado por duração
  (`usage.seconds`); ~0,8–2 s por áudio curto.

## 7.2 Leitura de comprovantes (V1.4)

- `apps/ai/vision.extract_receipt(image, content_type, context)`: Responses API, **1 chamada**,
  `input_image` com data URL da imagem já normalizada (sem URL pública/Storage, sem chaves),
  `detail="high"` (suficiente para texto pequeno nos testes; `original` não é usado),
  `store=False`, schema estrito, sem tools, `OPENAI_VISION_MODEL` / `OPENAI_VISION_TIMEOUT_SECONDS`
  (+ `OPENAI_REASONING_EFFORT` opcional). Sem OCR separado.
- Schema `receipt-v1.4`: `is_financial_document` (bool), `document_type` (`RECEIPT`,
  `PAYMENT_PROOF`, `INVOICE`, `OTHER`), `transaction_type` (`EXPENSE`, `INCOME`, `UNKNOWN`),
  `amount` (string decimal|null — o **total final**), `currency` (`BRL`, `OTHER`, `UNKNOWN`),
  `transaction_date`, `description` (rótulo genérico, sem nomes/CPF/CNPJ/chaves), `category_id`,
  `subcategory_id`. Não há campo de favorecido/pagador.
- Instruções: total final em vez de subtotal; desconto já aplicado; ignorar troco; nunca usar
  CPF, CNPJ, chave Pix, conta, autenticação ou id de transação como valor; direção do Pix só se
  explícita; não inventar data.
- Django revalida tudo (`actions.plan_receipt` + `create_transaction`); ver `WHATSAPP.md §7`.
- Validado ao vivo (`gpt-6-luna`, effort `none`) com comprovantes sintéticos: total escolhido em
  cupom com subtotal/desconto/troco/CPF/CNPJ; Pix enviado/recebido; USD, paisagem e Pix ambíguo
  recusados. ~1.400–1.600 tokens de entrada (imagem incluída), ~50–70 de saída, 2–5 s.

## 8. Testes

Unitários com cliente fake (`apps/ai/tests`); integração do pipeline com `interpret_message`
mockado (`apps/whatsapp/tests/test_pipeline.py`); rede real bloqueada na suíte.
Smoke test real é manual (`interpret_message` sem persistir), ver `TESTING.md`.
