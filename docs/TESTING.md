# Testes

## 1. Ferramentas (previstas para a V1.0)

- `pytest` + `pytest-django` como runner.
- PostgreSQL real em testes (container local/CI). SQLite é proibido: constraints,
  `NUMERIC`, `ArrayField` e índices parciais precisam se comportar como em produção.
- `ruff` para lint/format.
- Sem rede em testes padrão: OpenAI e Evolution sempre mockados.

Dependências serão adicionadas na implementação da V1.0, não antes.

## 2. Organização

```
apps/<app>/tests/      testes de unidade e de serviço do app
tests/                 integração ponta a ponta (webhook → resposta)
tests/fixtures/        payloads Evolution anonimizados, respostas de IA fixas
```

- Nome: `test_<comportamento>_<condição>` (ex.: `test_member_cannot_view_other_member_transactions`).
- Fábricas simples (funções ou `factory_boy` se justificado). Sem fixtures globais mágicas.
- Datas: congelar "agora" (ex.: `time-machine`/`freezegun` se adicionados, ou injeção de
  `reference_date`). Testes não podem depender do dia em que rodam.

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

### Categorias
- Somente slugs ativos aceitos; `kind` compatível; subcategoria pertence à categoria;
  fallback aplicado; IA não cria categoria.

### Idempotência
- Mesmo webhook entregue 2x → 1 `InboundMessage`, 1 `Transaction`, 1 resposta.
- Constraint de `idempotency_key` impede duplicata mesmo chamando o service direto 2x.

### Webhook
- Sem segredo / segredo errado → rejeitado sem efeitos.
- `fromMe`, grupo, número desconhecido → ignorados sem chamar IA.
- Payload malformado → `400`, sem exceção não tratada.

### Contrato de IA
- Parse de respostas válidas do schema; respostas inválidas/maliciosas tratadas.
- Erro/timeout da OpenAI → mensagem genérica, nenhuma transação.

### Soft delete
- Transação desfeita não aparece em consultas nem totais.

## 4. Testes com serviços reais

Chamadas reais à OpenAI/Evolution apenas em testes marcados (ex.: `@pytest.mark.live`),
desativados por padrão, executados manualmente com credenciais de desenvolvimento.
Nunca rodar contra o banco de produção.

## 5. Critério mínimo para merge

- Todos os testes passam; lint sem erros.
- Mudança em autorização, dinheiro, datas ou idempotência acompanha teste novo
  que falharia sem a mudança.
- Bug corrigido acompanha teste de regressão.
