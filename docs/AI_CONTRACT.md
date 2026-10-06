# Contrato de IA

Como o app `ai` usa a OpenAI e o que o Django faz com a resposta.
Regras de negócio aplicadas na validação: [`DOMAIN.md`](DOMAIN.md).

## 1. Papel da IA

A IA **interpreta** uma mensagem em linguagem natural e devolve uma **proposta estruturada**.
A proposta é entrada não confiável: o Django valida, resolve referências e autoriza.

A IA nunca: acessa banco, gera/executa SQL, calcula totais, decide permissões,
cria categorias, ou tem efeito colateral. Não há tool calling/function calling que execute
ações; a única saída é o objeto estruturado.

## 2. Integração

- SDK oficial da OpenAI com **Structured Outputs** (JSON Schema estrito). Proibido parsear
  texto livre com regex como mecanismo principal.
- Modelo: `OPENAI_MODEL` (env). Timeout: `OPENAI_TIMEOUT_SECONDS`. Sem modelo hardcoded.
- Não persistir conversas no provedor quando a API oferecer essa opção (ex.: `store=false`).
- Erro, timeout ou resposta fora do schema → tratar como `UNKNOWN` técnico, registrar
  erro sem conteúdo sensível, responder mensagem genérica ao usuário. Nunca criar transação.
- Interface do app (indicativa):

```python
def interpret_message(text: str, context: InterpretationContext) -> Interpretation: ...
```

`InterpretationContext` contém apenas: `reference_date` (hoje em America/Sao_Paulo,
ISO), `timezone`, catálogo de categorias/subcategorias **ativas** (slug, nome, aliases,
kind) e, a partir da V1.2, apelidos dos membros da família visíveis para o ator.

Dados que **não** vão para a IA: IDs internos, telefones, transações de terceiros,
totais, papéis/permissões, segredos.

## 3. Intenções

| Intenção | Versão | Descrição |
|---|---|---|
| `CREATE_TRANSACTION` | V1.0 | Registrar despesa ou receita |
| `QUERY_TRANSACTIONS` | V1.0 | Total/listagem por tipo, categoria, período |
| `HELP` | V1.0 | Usuário pede ajuda/instruções |
| `UNKNOWN` | V1.0 | Não identificado ou fora do escopo |
| `UPDATE_TRANSACTION` | V1.1 | Editar transação própria |
| `UNDO_TRANSACTION` | V1.1 | Desfazer transação própria |
| `COMPARE_PERIODS` | V1.1/V1.5 | Comparar totais entre períodos |
| `TOP_EXPENSES` | V1.1/V1.5 | Maiores gastos |
| `CATEGORY_BREAKDOWN` | V1.5 | Distribuição por categoria |
| `FAMILY_QUERY` | V1.2 | Consulta consolidada/de membro da família |

O enum do schema contém apenas as intenções habilitadas na versão corrente. Intenção
reconhecida mas não habilitada → resposta "ainda não disponível".

## 4. Schema de saída (V1.0)

Todos os campos presentes; ausência representada por `null` (requisito de schema estrito).

| Campo | Tipo | Notas |
|---|---|---|
| `intent` | enum | Seção 3 |
| `transaction_type` | `"EXPENSE"` \| `"INCOME"` \| null | |
| `amount` | string \| null | Decimal como string, ponto como separador: `"10.00"`, `"1234.5"`. **Nunca número JSON.** |
| `description` | string \| null | Texto curto do usuário ("gasolina") |
| `category` | string \| null | **slug** de categoria do catálogo fornecido |
| `subcategory` | string \| null | **slug** de subcategoria do catálogo fornecido |
| `transaction_date` | string \| null | ISO `YYYY-MM-DD`, resolvida a partir de `reference_date`; null se não mencionada |
| `start_date` | string \| null | ISO, consultas |
| `end_date` | string \| null | ISO, consultas |
| `scope` | `"SELF"` \| `"FAMILY"` \| `"MEMBER"` \| null | O que o usuário **pediu**; não autoriza nada |
| `target_user` | string \| null | Nome/apelido mencionado; resolvido e autorizado pelo Django |

Exemplos:

```jsonc
// "10 reais de gasolina"  (reference_date = 2026-10-06)
{"intent":"CREATE_TRANSACTION","transaction_type":"EXPENSE","amount":"10.00",
 "description":"gasolina","category":"transporte","subcategory":"combustivel",
 "transaction_date":null,"start_date":null,"end_date":null,"scope":null,"target_user":null}

// "Quanto gastei de transporte esse mês?"
{"intent":"QUERY_TRANSACTIONS","transaction_type":"EXPENSE","amount":null,
 "description":null,"category":"transporte","subcategory":null,"transaction_date":null,
 "start_date":"2026-10-01","end_date":"2026-10-06","scope":"SELF","target_user":null}
```

Mudanças no schema são mudanças de contrato: atualizar este documento, testes de
contrato e `CHANGELOG.md`.

## 5. Validação no Django (obrigatória)

Após o schema validar o formato, o Django aplica:

1. `intent` habilitada na versão atual.
2. `amount` → `Decimal` (rejeitar NaN, infinito, negativo, zero, > 2 casas após
   quantização, > `TRANSACTION_MAX_AMOUNT`).
3. `category`/`subcategory` → lookup por slug entre **ativas**; `kind` compatível com
   `transaction_type`; subcategoria pertence à categoria. Falha → regras de fallback de `DOMAIN.md`.
4. Datas → `date` válida, regras de `DOMAIN.md §4` (sem futuro; `start_date ≤ end_date`).
5. `scope`/`target_user` → resolvidos contra membros reais; autorização por
   `families.policies` usando o **ator**. Nunca usar o valor da IA como decisão.
6. Campos irrelevantes para a intenção são ignorados.

## 6. Prompt

- Prompts versionados em código no app `ai` (não em banco), com identificador de versão
  registrado junto à interpretação.
- Conteúdo do system prompt: tarefa de extração, idioma pt-BR, data de referência,
  catálogo permitido, regras "escolha apenas slugs listados; use null quando não souber;
  não invente valores; valores como string decimal".
- Texto do usuário vai em mensagem separada, nunca concatenado nas instruções.
  Tratar tentativas de prompt injection como texto comum: o pior resultado possível
  deve ser uma proposta inválida rejeitada pela validação.

## 7. Testes

- Testes unitários do app `ai` usam cliente mockado com respostas fixas (sem rede).
- Testes de validação cobrem respostas maliciosas/inválidas (categoria inexistente,
  `amount` negativo, `scope: FAMILY` vindo de MEMBER, etc.).
- Avaliação com chamadas reais à OpenAI é opcional, manual, marcada e fora do CI padrão.
  Ver `TESTING.md`.
