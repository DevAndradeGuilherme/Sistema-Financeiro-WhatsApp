# AGENTS.md

Guia operacional para coding agents (Claude Code, Codex e outros).
Leia este arquivo inteiro. Depois leia **somente** os documentos relevantes para a tarefa.
O repositório é a fonte de verdade: se um prompt contradizer `docs/`, pare e aponte o conflito.

## O que é o projeto

Controle financeiro pessoal e familiar operado pelo WhatsApp.
Usuário escreve "10 reais de gasolina" ou "quanto gastei de transporte esse mês?" e o sistema
registra ou consulta transações, respondendo pelo WhatsApp.

Stack: Python, Django, PostgreSQL (Supabase), Evolution API, OpenAI,
Docker, Gunicorn, Nginx. Arquitetura: **monólito modular**.

## Princípio central

```
IA interpreta.  Django valida e autoriza.  PostgreSQL calcula e persiste.  WhatsApp comunica.
```

## Invariantes (nunca violar)

1. A IA nunca acessa o banco, gera/executa SQL, calcula totais finais, decide permissões,
   cria categorias ou executa operações sem validação server-side.
2. Saída da IA é **proposta não confiável**. Campos como `scope`, `target_user`, `role`
   vindos da IA nunca são usados para autorizar nada.
3. Dinheiro é `Decimal` em Python e `NUMERIC(14,2)` no PostgreSQL. **Nunca `float`**.
4. Data financeira (`transaction_date`, tipo `date`) ≠ timestamp (`created_at`, timezone-aware).
   Timezone de negócio: `America/Sao_Paulo`. `USE_TZ = True`.
5. Categorias e subcategorias são fixas, mantidas apenas pelo administrador do sistema.
6. MEMBER acessa apenas os próprios dados. Family ADMIN **lê** dados dos membros da sua
   família, mas **nunca edita nem desfaz** transações de terceiros.
7. Webhooks são idempotentes: a mesma mensagem externa nunca gera duas transações
   (garantido por constraint de unicidade no banco, não apenas por código).
8. Áudio e imagem reutilizam o mesmo pipeline/domínio do texto. Nunca duplicar regras financeiras.
9. Segredos nunca são versionados nem logados. Entrada externa é sempre não confiável.
10. Exclusão de transação é soft delete. Consultas padrão ignoram registros excluídos.

Detalhes e justificativas: `docs/ARCHITECTURE.md` e `docs/SECURITY.md`.

## Fora do escopo (não adicionar sem decisão documentada)

- Microserviços, Celery, Redis, LangChain, frameworks de agentes.
- Dashboard/frontend próprio (Django Admin atende a administração).
- Funcionalidades fora do `docs/ROADMAP.md`.
- Dependências novas sem justificativa no PR/commit.

Se uma necessidade técnica real surgir, registre a decisão em `docs/ARCHITECTURE.md`
(seção "Registro de decisões") antes de implementar.

## Mapa de documentos — leia conforme a tarefa

| Tarefa | Leia |
|---|---|
| Qualquer tarefa | este arquivo + `docs/ROADMAP.md` (para saber a versão atual) |
| Criar/alterar app, módulo, camada | `docs/ARCHITECTURE.md` |
| Models, migrations, constraints, queries | `docs/DATABASE.md` + `docs/DOMAIN.md` |
| Regras de negócio, transações, categorias, famílias, permissões | `docs/DOMAIN.md` |
| Prompts, schema de saída, integração OpenAI | `docs/AI_CONTRACT.md` |
| Webhook, Evolution API, envio de respostas, mídia | `docs/WHATSAPP.md` |
| Autenticação, autorização, segredos, logs, dados sensíveis | `docs/SECURITY.md` |
| Escrever ou revisar testes | `docs/TESTING.md` |
| Docker, Gunicorn, Nginx, variáveis de ambiente, Supabase | `docs/DEPLOYMENT.md` |
| Health/readiness, recuperação de mensagens, retenção, incidentes | `docs/OPERATIONS.md` |
| Auditoria/revisão (Codex) | `docs/SECURITY.md` + `docs/TESTING.md` + doc do módulo afetado |

Não carregue toda a pasta `docs/` sem necessidade.

## Estrutura do código (prevista)

```
config/            settings/{base,runtime,test}.py, urls, wsgi
apps/
  accounts/        User customizado, telefone E.164
  families/        Family, FamilyMember, políticas de acesso
  categories/      Category, Subcategory, aliases
  transactions/    Transaction, services, selectors
  ai/              cliente OpenAI, schemas, prompts (sem acesso a models de domínio)
  whatsapp/        webhook, InboundMessage, cliente Evolution, orquestração do pipeline
common/            utilitários sem regra de negócio (dinheiro, datas, telefone, RLS, health)
tests/             testes transversais/integração; testes de unidade ficam em cada app
docs/
```

Dentro de cada app, quando fizer sentido: `models.py`, `services.py` (escrita),
`selectors.py` (leitura), `policies.py` (autorização), `admin.py`, `tests/`.
Não criar pasta global de services. Não criar camadas/abstrações sem uso real.

## Comandos

```bash
docker compose --profile test up -d test-db   # banco de testes local (nunca Supabase)
pytest                                         # testes (config.settings.test)
ruff check . && ruff format --check .          # lint
python manage.py migrate | seed_categories     # banco DB_* (config.settings.runtime)
```

## Convenções

- Código, identificadores, nomes de models/campos e commits: **inglês**.
- Mensagens ao usuário final e documentação: **português (pt-BR)**.
- Commits: Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
- Views/webhooks são finos: parse → chamar service/selector → responder.
- Services recebem o **ator** (usuário autenticado pelo telefone) explicitamente.
- Selectors sempre filtram por escopo autorizado; nunca retornar queryset "global" para o canal.
- Escrita multi-etapa em `transaction.atomic()`.
- Modelo da OpenAI vem de variável de ambiente (`OPENAI_MODEL`), nunca hardcoded.
- Configuração via variáveis de ambiente; ver `.env.example`.

## Fluxo de trabalho do agent

1. Identifique a versão do roadmap a que a tarefa pertence. Não implemente versões futuras.
2. Leia os documentos indicados no mapa acima.
3. Implemente a menor mudança que resolve a tarefa, seguindo as fronteiras dos módulos.
4. Escreva/atualize testes (ver `docs/TESTING.md`). Regras de autorização e dinheiro
   exigem testes explícitos.
5. Rode testes e lint antes de concluir.
6. Atualize a documentação afetada e adicione entrada em `docs/CHANGELOG.md`.
7. Nunca commite `.env`, chaves, dumps de banco ou mídias de usuários.

## Definição de pronto (DoD)

Uma tarefa só está pronta quando:

- [ ] Respeita todos os invariantes acima.
- [ ] Tem testes cobrindo caminho feliz, validação e autorização (quando aplicável).
- [ ] Testes passam localmente contra PostgreSQL.
- [ ] Migrations geradas, revisadas e reversíveis quando possível.
- [ ] Nenhum `float` em valores monetários; nenhum segredo em código/log.
- [ ] Documentação e `docs/CHANGELOG.md` atualizados se comportamento ou contrato mudou.
- [ ] Nenhuma dependência nova sem justificativa.

## Papéis dos agents

- **Claude Code**: implementação de features seguindo este guia.
- **Codex**: auditoria, revisão, segurança, debugging, regressões, correções cirúrgicas.
  Ao revisar, priorize: autorização, idempotência, dinheiro/datas, vazamento de segredos/dados.
