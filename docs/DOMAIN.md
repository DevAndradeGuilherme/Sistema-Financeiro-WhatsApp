# Domínio

Regras de negócio e modelo conceitual. Tipos e constraints físicas: [`DATABASE.md`](DATABASE.md).
Interpretação pela IA: [`AI_CONTRACT.md`](AI_CONTRACT.md).

## 1. Glossário

| Termo | Significado |
|---|---|
| Ator | Usuário que enviou a mensagem, identificado pelo telefone. Toda operação é feita "em nome do ator". |
| Dono (owner) | Usuário a quem a transação pertence. Sempre o ator que a criou. |
| Transação | Registro de uma despesa (`EXPENSE`) ou receita (`INCOME`). |
| Data financeira | Dia em que o gasto/receita ocorreu (`transaction_date`, tipo `date`). |
| Timestamp | Momento técnico de criação/alteração (`created_at`, `updated_at`, timezone-aware). |
| Administrador do sistema | Staff/superuser do Django Admin. Mantém catálogo e usuários. **Não** é o ADMIN de família. |
| Family ADMIN | Papel dentro de uma família. Lê dados dos membros; não altera dados de terceiros. |

## 2. Modelo conceitual

```
User 1 ── 0..1 FamilyMember N ── 1 Family
User 1 ── N Transaction
Category 1 ── N Subcategory
Transaction N ── 1 Category
Transaction N ── 0..1 Subcategory
```

### User (`accounts`)

- Usuário Django customizado (`AbstractUser`), usado também para login no Django Admin.
- `phone_number`: canônico em E.164 (`+5531999998888`), único, opcional
  (staff pode não ter WhatsApp). Usuário sem telefone não usa o canal.
- Cadastro de usuários do canal é feito pelo administrador do sistema no Django Admin
  no MVP. Não há auto-cadastro via WhatsApp.
- Usuário inativo (`is_active=False`) não é atendido pelo canal.

### Family e FamilyMember (`families`)

- `Family`: nome e timestamps.
- `FamilyMember`: liga `User` a `Family` com `role ∈ {ADMIN, MEMBER}`.
- Decisão inicial: **um usuário pertence a no máximo uma família** (unicidade em `user`).
  Revisável na V1.2 com registro em `ARCHITECTURE.md`.
- Uma família tem **exatamente um ADMIN**: no máximo um garantido pelo banco (unique parcial
  `family_single_admin`); "ao menos um" garantido por `create_family` (cria família + ADMIN
  atomicamente) e pelo formulário do Django Admin.
- Estrutura existe desde a V1.0; funcionalidades familiares são liberadas na V1.2.
  Antes disso, todo usuário opera como se estivesse sozinho.
- `Transaction.family` registra a família do dono **no momento da criação**; é derivada pelo
  service a partir do membership, nunca escolhida livremente.
- Visibilidade familiar (V1.2) usa a regra mais restritiva: o ADMIN só vê transações cuja
  `family` é a sua **e** cujo dono ainda é membro dela. Se um membro sai, o ADMIN deixa de ver
  as transações dele. "Dono" é sempre `Transaction.user`; ADMIN nunca é dono de dados alheios.

### Category e Subcategory (`categories`)

- Catálogo **fixo**, mantido apenas pelo administrador do sistema via Django Admin
  (catálogo inicial via `python manage.py seed_categories`, idempotente por `slug`, nunca
  sobrescreve alterações do administrador).
- A IA nunca cria, renomeia ou sugere categorias novas; ela só escolhe entre as ativas.
- `Category`: `name`, `slug` (chave estável usada no contrato com a IA), `kind ∈ {EXPENSE, INCOME, BOTH}`,
  `description`, `aliases` (termos que ajudam a classificação), `is_active`, `sort_order`.
  `BOTH` aceita despesas e receitas. Categoria em uso não pode mudar para um `kind`
  incompatível com suas transações.
- `Subcategory`: pertence a uma `Category` (herda o `kind` dela); mesmos campos de `name`, `slug`,
  `description`, `aliases`, `is_active`. Subcategoria em uso não pode mudar de categoria.
- Hierarquia de **dois níveis** apenas.
- Categorias de fallback: "Outros gastos" (EXPENSE) e "Outras receitas" (INCOME). A marcação
  explícita de fallback será adicionada junto com a integração de IA.
- Desativar é a forma padrão de "remover". Exclusão física só se não houver transações
  referenciando (FK `PROTECT`). Categoria inativa não é oferecida à IA e não aceita novas
  transações, mas continua válida em transações antigas e em consultas.

Catálogo inicial (conceitual, ajustável pelo administrador):

| Kind | Categorias |
|---|---|
| EXPENSE | Transporte, Alimentação, Moradia, Saúde, Educação, Lazer, Compras, Assinaturas, Contas, Investimentos, Outros gastos |
| INCOME | Salário, Outras receitas |

Exemplo de subcategorias — Transporte: Combustível, Uber, Transporte público, Pedágio,
Estacionamento, Manutenção.

> "Investimentos" é tratado como EXPENSE (saída de caixa) no catálogo inicial. Rendimentos
> recebidos entram em "Outras receitas". Mudança exige decisão do administrador.

### Transaction (`transactions`)

Campos conceituais: `owner`, `type`, `amount`, `category`, `subcategory?`, `description`,
`transaction_date`, `source`, `family?`, `idempotency_key?`, `created_at`, `updated_at`, `deleted_at?`.
No código, o dono é o campo `user`. `source ∈ {TEXT, AUDIO, IMAGE, MANUAL, ADMIN}` (agnóstico de canal).

## 3. Política financeira

1. `amount` é `Decimal`, **estritamente positivo**, 2 casas decimais, moeda BRL.
   O sinal é dado por `type`. Nunca armazenar valores negativos.
2. Limite superior configurável (`TRANSACTION_MAX_AMOUNT`) para barrar erros de
   interpretação ("10 mil" lido como 10.000.000). Acima do limite: pedir confirmação/rejeitar.
3. `category.kind` deve ser igual a `transaction.type` ou `BOTH`.
4. `subcategory`, se presente, deve pertencer a `category`.
5. Categoria/subcategoria precisam estar ativas no momento da criação ou edição.
6. Categoria não reconhecida pela validação → fallback do `kind` correspondente, e a
   resposta informa a classificação usada.
7. Sem valor identificável → nenhuma transação é criada; o sistema pede o valor.
8. Saldo = soma de INCOME − soma de EXPENSE, calculado no banco.
9. Totais e agregações sempre por SQL (ORM), excluindo transações soft-deleted.

## 4. Datas

- "Hoje" = data local em `America/Sao_Paulo` no momento do processamento.
- Data mencionada pelo usuário é respeitada ("ontem", "sexta-feira", "dia 2").
- Sem data mencionada → hoje.
- Resolução de expressões relativas: a IA recebe a data de referência (hoje local) e devolve
  data absoluta ISO; o Django valida com as regras abaixo (ver `AI_CONTRACT.md`).
- Regras de validação:
  - `transaction_date` não pode estar no futuro em relação a hoje local.
  - Dia da semana sem qualificação ("sexta-feira") = a ocorrência mais recente, ≤ hoje.
  - "dia N" sem mês = dia N do mês corrente se ≤ hoje; senão, do mês anterior.
  - Data inválida/ambígua → pedir esclarecimento, não adivinhar.
- Períodos de consulta: intervalo fechado `[start_date, end_date]` sobre `transaction_date`.
  "esse mês" = dia 1 até hoje; "mês passado" = mês civil anterior completo.

## 5. Ciclo de vida da transação

| Operação | Versão | Regra |
|---|---|---|
| Criar | V1.0 | Ator vira `owner`. Idempotente por `idempotency_key`. |
| Consultar | V1.0 (próprias), V1.2 (família) | Sempre via selector com escopo autorizado. |
| Editar | V1.1 | Só o `owner`. Revalida todas as regras da seção 3. Não altera `owner`. |
| Desfazer | V1.1 | Só o `owner`. Aplica soft delete (`deleted_at`). |
| Soft delete | V1.1 | `deleted_at` preenchido; registro some de consultas e totais. Sem exclusão física pelo canal. |

Alvo de edição/desfazer no V1.1: por padrão a **última transação ativa do próprio ator**;
referências mais específicas exigem contexto conversacional mínimo (V1.1).
Restaurar transação desfeita não está no escopo atual.

## 6. Autorização

Implementada em `policies.py` (Django). **Nunca** derivada de campos retornados pela IA.

| Ação | Próprias | De outro membro (sou MEMBER) | De outro membro (sou Family ADMIN) | Fora da família |
|---|---|---|---|---|
| Criar | ✅ | ❌ (não existe criar em nome de outro) | ❌ | ❌ |
| Consultar/agregar | ✅ | ❌ | ✅ (somente leitura) | ❌ |
| Consolidado da família | — | ❌ | ✅ | ❌ |
| Editar | ✅ | ❌ | ❌ | ❌ |
| Desfazer/excluir | ✅ | ❌ | ❌ | ❌ |

Funções de referência:

- `can_view_user_data(actor, target_user) -> bool`: `actor == target_user` ou
  (ambos na mesma família e `actor` é ADMIN dela).
- `can_view_family(actor, family) -> bool`: `actor` é ADMIN da família.
- `can_modify_transaction(actor, transaction) -> bool`: `transaction.user == actor` (dono).

Negação de acesso gera resposta neutra ("Você não tem permissão para consultar esses dados"),
sem revelar se o alvo existe.

## 7. Identificação no canal

- Remetente é identificado exclusivamente pelo telefone normalizado do payload
  (ver `WHATSAPP.md`), comparado a `User.phone_number`.
- Número desconhecido ou usuário inativo: a mensagem é registrada, **não** é enviada à IA,
  e nenhuma transação é criada.
