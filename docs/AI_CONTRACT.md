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
- Validado ao vivo com `gpt-6-luna` + `OPENAI_REASONING_EFFORT=none`: ~860 tokens de entrada,
  ~60–70 de saída, 0 de raciocínio, 1–3 s por mensagem. `low` não melhorou a qualidade nos
  casos de teste e gastou 20–55 tokens de raciocínio a mais.
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
| `QUERY_TRANSACTIONS` | V1.0 ✅ | `TOTAL` (gastos ou receitas, opcionalmente por categoria/subcategoria) ou `BALANCE` no período |
| `HELP` | V1.0 ✅ | Ajuda (resposta local fixa) |
| `UNKNOWN` | V1.0 ✅ | Não identificado, fora do escopo ou sobre dados de outra pessoa |
| `UPDATE_TRANSACTION`, `UNDO_TRANSACTION` | V1.1 | — |
| `COMPARE_PERIODS`, `TOP_EXPENSES` | V1.1/V1.5 | — |
| `CATEGORY_BREAKDOWN` | V1.5 | — |
| `FAMILY_QUERY` | V1.2 | — |

O enum do schema contém apenas as intenções da versão corrente.

## 4. Schema de saída (`contract_version v1.0`)

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
| `query_kind` | `TOTAL` \| `BALANCE` \| null | |
| `start_date`, `end_date` | string \| null | `YYYY-MM-DD`, intervalo fechado |

Sem `scope`, `target_user`, `confidence` ou qualquer campo de usuário/autorização na V1.0:
toda consulta é do próprio remetente.

```jsonc
// "10 reais de gasolina" (hoje = 2026-10-06)
{"intent":"CREATE_TRANSACTION","transaction_type":"EXPENSE","amount":"10.00",
 "description":"gasolina","category_id":2,"subcategory_id":1,"transaction_date":null,
 "query_kind":null,"start_date":null,"end_date":null}

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
3. `amount` → `Decimal` via `common.money.to_decimal` (sem float); faltando → pede o valor;
   inválido → resposta; `create_transaction` revalida (> 0, 2 casas, limite).
4. Categoria (criação): id precisa existir, estar ativo e aceitar o tipo; subcategoria precisa
   estar ativa, aceitar o tipo e pertencer à categoria (incompatível → descartada; sozinha →
   implica sua categoria). Sem categoria válida → **fallback explícito** do tipo
   (`Category.is_fallback`, ver `DOMAIN.md`).
5. Categoria (consulta): id inexistente/incompatível → resposta "não encontrei", nunca fallback.
6. Datas: ISO estrito; criação sem data → hoje; data futura rejeitada pelo service. Consulta sem
   período → mês corrente até hoje; só início → até hoje; só fim → o próprio dia; `start ≤ end`.
7. Totais: somente `apps.transactions.selectors` (`get_total`, `get_period_totals`).

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

## 8. Testes

Unitários com cliente fake (`apps/ai/tests`); integração do pipeline com `interpret_message`
mockado (`apps/whatsapp/tests/test_pipeline.py`); rede real bloqueada na suíte.
Smoke test real é manual (`interpret_message` sem persistir), ver `TESTING.md`.
