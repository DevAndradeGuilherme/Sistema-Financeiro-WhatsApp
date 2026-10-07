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
common/tests/          helpers (dinheiro, datas, telefone) e infraestrutura (RLS, health)
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
