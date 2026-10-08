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
- Django Admin é a interface operacional inicial: cadastro, dados básicos e ativação/desativação.
  O cadastro sugere autenticação por senha desabilitada (`unusable password`), suficiente para
  WhatsApp; senha de login só quando escolhida explicitamente, sem senha padrão ou hash na tela.
  Telefone passa pelo mesmo `common.phone.normalize_phone` do canal, antes da validação de
  tamanho/unicidade do formulário (incluindo o 9º dígito brasileiro).
- Desativação é preferível a exclusão: mantém memberships, mensagens e todo o histórico.
  Exclusão de usuários pelo Admin é bloqueada, inclusive sem transações, pois o CASCADE do
  membership poderia deixar uma família sem ADMIN e o SET_NULL perderia identidade de mensagens.

### Family e FamilyMember (`families`)

- `Family`: nome e timestamps.
- `FamilyMember`: liga `User` a `Family` com `role ∈ {ADMIN, MEMBER}`.
- Decisão inicial: **um usuário pertence a no máximo uma família** (unicidade em `user`).
  Revisável na V1.2 com registro em `ARCHITECTURE.md`.
- Uma família tem **exatamente um ADMIN**: no máximo um garantido pelo banco (unique parcial
  `family_single_admin`); "ao menos um" garantido por `create_family` (cria família + ADMIN
  atomicamente) e pelo formulário do Django Admin.
- No Admin, criar família exige ADMIN inicial; adicionar usuário sempre cria MEMBER. Inclusão
  reutiliza os services existentes. `FamilyMemberAdmin` e a relação de membros são somente leitura;
  operações ficam na página da família. Famílias não podem ser excluídas pelo Admin.
- **Transferir administração** exige um MEMBER atual da mesma família. O service recebe o
  operador Django, exige permissões e executa em `transaction.atomic()`: lock da família,
  validação de exatamente um ADMIN, rebaixamento do atual, promoção do destino e validação final.
  Inclusão, remoção e transferência compartilham o lock `FOR NO KEY UPDATE` da família; inclusão
  também trava o usuário para serializar tentativas de entrada em famílias diferentes.
  O lock não bloqueia referências por FK do ledger. Remover ADMIN isoladamente é proibido.
- Remover MEMBER não modifica `Transaction.family` nem transfere/apaga histórico. ADMIN/MEMBER
  nunca concedem `is_staff`, `is_superuser`, grupos ou permissões Django.
- Estrutura existe desde a V1.0; funcionalidades familiares são liberadas na V1.2.
  Antes disso, todo usuário opera como se estivesse sozinho.
- `Transaction.family` registra a família do dono **no momento da criação**; é derivada pelo
  service a partir do membership, nunca escolhida livremente.
- Visibilidade familiar (V1.2, implementada) segue o **snapshot histórico**: dado familiar é
  toda transação com `Transaction.family == família` (registrada enquanto o dono era membro).
  Pertencer à família hoje **não** dá acesso a transações registradas antes de entrar (sem
  `family`) nem às registradas em outra família; quem sai continua no histórico da família
  antiga e não leva esse histórico para a nova. Consulta por membro (`FAMILY_MEMBER`) exige que
  ele seja membro **atual** e lê só `user=membro AND family=família`.
  (Substitui a regra provisória da V1.0 "e cujo dono ainda é membro".)
- "Dono" é sempre `Transaction.user`; o ADMIN nunca é dono de dados alheios.

### Category e Subcategory (`categories`)

- Catálogo **fixo**, mantido apenas pelo administrador do sistema via Django Admin
  (catálogo inicial via `python manage.py seed_categories`, idempotente por `slug`, nunca
  sobrescreve alterações do administrador).
- A IA nunca cria, renomeia ou sugere categorias novas; ela só escolhe entre as ativas.
- `Category`: `name`, `slug` (chave estável usada pelo seed; a IA referencia o `id` inteiro), `kind ∈ {EXPENSE, INCOME, BOTH}`,
  `description`, `aliases` (termos que ajudam a classificação), `is_active`, `sort_order`.
  `BOTH` aceita despesas e receitas. Categoria em uso não pode mudar para um `kind`
  incompatível com suas transações.
- `Subcategory`: pertence a uma `Category` (herda o `kind` dela); mesmos campos de `name`, `slug`,
  `description`, `aliases`, `is_active`. Subcategoria em uso não pode mudar de categoria.
- Hierarquia de **dois níveis** apenas.
- **Fallback explícito**: `Category.is_fallback`. Exatamente uma por tipo (EXPENSE e INCOME),
  garantido no banco (unique parcial por `kind`; fallback precisa estar ativa e não pode ser
  `BOTH`). Identificada pela flag, nunca pelo nome. O seed marca "Outros gastos"/"Outras
  receitas" apenas se ainda não houver fallback do tipo (não sobrescreve escolha do admin).
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
6. Na criação, categoria proposta inexistente/inativa/incompatível → fallback do tipo
   (flag `is_fallback`), e a resposta informa a categoria usada. Fallback não esconde erro
   técnico: sem fallback configurado, a mensagem falha sem efeito.
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

### Análises (V1.5)

Calculadas sob demanda em `transactions/analytics.py`, sobre o `ReadScope` autorizado (§6) —
mesmas regras de SELF/FAMILY/FAMILY_MEMBER da V1.2. Ignoram soft-deleted, usam os valores
atuais (após edição) e tratam TEXT/AUDIO/IMAGE igualmente. Nada é persistido. Respostas são
fatos neutros, sem aconselhamento financeiro.

- **Resumo do período (`PERIOD_SUMMARY`)**: receitas, despesas, saldo (pode ser negativo),
  quantidade de receitas/despesas, gasto médio (despesas ÷ quantidade), maior gasto, maior
  receita e as 3 principais categorias de gasto. Percentuais: despesas/receitas e economia
  (`(receitas − despesas) ÷ receitas`); acima de 100% ou negativos são mostrados como são;
  **com receitas = 0 nenhum percentual é mostrado**.
- **Evolução mensal (`MONTHLY_TREND`)**: meses civis, janela de 1 a 12 meses normalmente
  terminando no mês corrente (incluído, até hoje); em follow-up com `end_date`, essa data apenas
  ancora o mês final. `months` define a largura e prevalece sobre qualquer `start_date` auxiliar.
  O contexto persiste a janela efetiva. Padrão 6; pedido acima de 12 é limitado a 12 e a resposta
  diz isso; valor nulo/zero/negativo → padrão. Métricas `EXPENSE`, `INCOME`, `BALANCE`. Meses sem
  movimentação contam como zero (inclusive na média mensal).
- **Variação por categoria (`CATEGORY_TREND`)**: total por categoria no período A × período B,
  em uma única query. `INCREASE` = categorias que aumentaram (maior aumento primeiro);
  `DECREASE` = que diminuíram (maior queda primeiro); `ALL` = qualquer variação (maior variação
  absoluta primeiro). Período anterior = 0 → sem percentual ("sem movimento no período anterior").
- Percentuais: `common.money.percent` — `Decimal`, 1 casa, `ROUND_HALF_UP`, `None` quando o
  denominador é zero (também usado pelo `COMPARE`).

**Comportamento conhecido / limitação (`CATEGORY_TREND`)**: sem períodos explícitos, segue a
mesma semântica do `COMPARE`: **mês corrente até hoje × mês anterior completo**. No início do
mês o período atual é parcial, então a comparação favorece o mês anterior (tende a mostrar
reduções). Mantido por consistência com o `COMPARE`; mudar para comparação proporcional
(ex.: 1–7 × 1–7) exige decisão de produto e entrada no registro de decisões.

## 5. Ciclo de vida da transação

| Operação | Versão | Regra |
|---|---|---|
| Criar | V1.0 | Ator vira dono (`user`). Idempotente por `idempotency_key`. Gera `TransactionChange(CREATE)`. |
| Consultar | V1.0/V1.1 (próprias), V1.2 ✅ (família, ADMIN) | Via selector com `user=` remetente ou `scope=` autorizado (seção 6). |
| Editar | V1.1 ✅ | `update_transaction`: só o dono, só transação ativa. Campos: valor, descrição, categoria, subcategoria, data. **Nunca**: dono, família, tipo, origem, ids. Revalida as regras da seção 3. Gera `UPDATE` com antes/depois. |
| Desfazer | V1.1 ✅ | `undo_last_change`: ver abaixo. Gera `UNDO`. |
| Soft delete | V1.0 | `deleted_at`; some de consultas e totais. Gera `DELETE`. Sem exclusão física pelo canal. |

### Seleção do lançamento a editar (V1.1)

A IA descreve a referência (`último`, tipo, categoria, subcategoria, descrição, valor, data);
o Django busca **somente nas transações ativas do remetente**, com `transaction_date` nos
últimos 90 dias (`find_transactions`):

- "último" ou referência sem critérios → a transação registrada mais recentemente (do tipo,
  se informado);
- com critérios → 0 resultados: "não encontrei"; 2 ou mais: pede valor/data e **não altera
  nada**; exatamente 1: altera. Categoria/subcategoria na referência dispensam a descrição
  livre (o item já está identificado pelo catálogo).

### Política de desfazer (V1.1)

- "desfazer" desfaz a **última ação reversível ainda não desfeita do próprio usuário**:
  `CREATE` (→ soft delete) ou `UPDATE` (→ restaura os valores anteriores exatos).
- É uma pilha: criar 10 → mudar para 15 → desfazer (volta a 10) → desfazer (remove o registro)
  → desfazer ("nada para desfazer").
- `UNDO` e `DELETE` não são reversíveis; não há redo. Cada ação só pode ser desfeita uma vez
  (unicidade no banco). Ações de transações já excluídas são puladas.
- Transações criadas antes da V1.1 não têm registro de criação e não são desfeitas.
- Não há janela de tempo: a resposta sempre informa exatamente o que foi desfeito.

### Comprovantes (V1.4)

- Imagem de comprovante gera, no máximo, uma `create_transaction` do **próprio remetente**
  (`source=IMAGE`), com as mesmas regras do texto. Nunca edita, consulta ou desfaz por imagem.
- Imagens de comprovante são processadas de forma efêmera, em memória, e **não são retidas**.
  A imagem é só entrada transitória; o registro persistente é a `Transaction`.
- `ReceiptAttachment` guarda apenas metadados técnicos da mídia processada (tipo, tamanho,
  dimensões, SHA-256, tipo de documento, status) ligados à mensagem e, se houver, à transação.
  Nunca conteúdo visual, OCR, CPF/CNPJ, chave Pix, dados bancários ou favorecido/pagador.
  Soft delete / "desfazer" da transação **não** apaga esses metadados.
- `IMAGE_RETENTION = NONE` (imagem existe só durante o processamento). Retenção de
  `InboundMessage`, metadados, `Transaction` e auditoria segue `PENDING_PRODUCT_DECISION`.

### Auditoria

`TransactionChange`: quem (`user`), qual transação, ação, `before`/`after` (somente campos
financeiros; dinheiro como string), quando, e `origin` (ex.: `whatsapp:<instância>:<id da
mensagem>`, que identifica a `InboundMessage`). Somente leitura no Admin.

## 6. Autorização

Implementada no Django. **Nunca** derivada de campos retornados pela IA.
**Family ADMIN = somente leitura sobre terceiros.**

| Ação | Próprias | De outro membro (sou MEMBER) | De outro membro (sou Family ADMIN) | Fora da família |
|---|---|---|---|---|
| Criar | ✅ | ❌ (não existe criar em nome de outro) | ❌ | ❌ |
| Consultar/agregar | ✅ | ❌ | ✅ (somente leitura, snapshot histórico) | ❌ |
| Consolidado / ranking da família | — | ❌ | ✅ | ❌ |
| Editar | ✅ | ❌ | ❌ | ❌ |
| Desfazer/excluir | ✅ | ❌ | ❌ | ❌ |

Peças (V1.2):

- `families.policies.can_view_family(actor, family)`: `actor` é o ADMIN **atual** da família.
- `transactions.scopes`: única forma de obter um `ReadScope` (`SELF`, `FAMILY`,
  `FAMILY_MEMBER`); aplica a policy a cada consulta; membro resolvido por nome
  (`families.selectors.find_family_members_by_name`) **somente** entre os membros atuais da
  família do ADMIN, sem diferenciar acentos/maiúsculas, por nome completo, primeiro nome ou
  username. Nome que casa com mais de um membro → pede o nome completo, nunca escolhe.
  Autorização é verificada **antes** de resolver nomes (MEMBER nunca descobre quem existe).
- Selectors de leitura recebem `user=` (próprios dados) ou `scope=` (autorizado). Não existe
  selector por `user_id` arbitrário.
- `transactions.policies.can_modify_transaction(actor, transaction)`: só o dono. Usada por
  `update_transaction`, `undo_last_change` e `soft_delete_transaction`. Não existem services de
  criação/edição/exclusão/desfazer em nome de terceiros.
- Papel é revalidado a cada mensagem; contexto conversacional guarda semântica, nunca permissão.

Respostas de negação são locais e discretas (`whatsapp/messages.py`): "Essa consulta está
disponível apenas para o administrador da família.", "Você ainda não faz parte de uma família
cadastrada.", "Não encontrei esse membro na sua família.", "Você só pode registrar transações na
sua própria conta.", "Você só pode alterar suas próprias transações."

## 7. Identificação no canal

- Remetente é identificado exclusivamente pelo telefone normalizado do payload
  (ver `WHATSAPP.md`), comparado a `User.phone_number`.
- Número desconhecido ou usuário inativo: a mensagem é registrada, **não** é enviada à IA,
  e nenhuma transação é criada.
