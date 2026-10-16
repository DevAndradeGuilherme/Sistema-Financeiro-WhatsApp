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
| Desfazer | V1.1 ✅ | `undo_last_change`, só quando o texto pede a última ação: ver abaixo. Gera `UNDO`. |
| Soft delete | V1.0 | `deleted_at`; some de consultas e totais. Gera `DELETE`. Sem exclusão física pelo canal. Pelo canal: remoção do lançamento descrito (1 candidato) ou, com confirmação, de um conjunto (ver abaixo). |

### Seleção do lançamento a editar ou remover (V1.1; reforçada em 2026-10-15)

A IA descreve a referência (`último`, tipo, categoria, subcategoria, descrição, valor, data ou
período, "todos"); o Django busca **somente nas transações ativas do remetente**
(`find_transactions`) e aplica **todos** os critérios combinados por **E** (AND) — nunca
descarta nem relaxa um filtro para achar alguma coisa (descrição é aplicada mesmo junto de
categoria/subcategoria). Data citada é sempre `transaction_date` (nunca `created_at`). Sem
data/período, a busca cobre `transaction_date` dos últimos 90 dias.

- **Mutação sem alvo seguro → nada muda e o usuário é perguntado.** "desfazer", "remove",
  "apaga", "cancela" sozinhos **não** identificam nada: zero mutação, resposta pedindo valor e
  data (ou "desfaça o último lançamento"), qualquer que seja a proposta da IA.
- Texto também é lido pelo Django (`whatsapp/hints.py`, defesa em profundidade, sem NLP
  paralelo): tipo ("entrada"/"receita" × "gasto"), números, datas explícitas ("07/10", "dia 5",
  "hoje", "ontem", "de outubro"...), categoria/subcategoria do catálogo (nomes e aliases
  inequívocos), membro da família, "último", "todos" e marcadores de correção. Em `UPDATE` e
  `UNDO`: critério citado no texto mas omitido ou contradito pela IA → pergunta
  (`TARGET_UNCLEAR`), sem alterar nada. Em `UPDATE`, cada número/data/item citado precisa ser
  critério **ou** valor novo. Critério de valor/data proposto pela IA sem aparecer no texto
  também é recusado. O alvo precisa de pelo menos um critério **comprovado pelo texto** (ou
  "último"); critérios apenas inferidos pela IA podem restringir, nunca bastam sozinhos.
- Texto que cita **outro membro da família** ("apague a entrada de 77 da Maria") bloqueia
  qualquer edição/remoção (`FORBIDDEN_OTHER_ACCOUNT`), mesmo que a IA omita o nome — inclusive
  para o ADMIN. Consultas familiares não mudam.
- 0 resultados: "Não encontrei uma entrada de R$ 50,00 em 07/10." — nada muda.
- 1 resultado: a operação individual prossegue.
- 2 ou mais: lista até 3 candidatos e pergunta qual; **nada muda**. Nunca escolhe o primeiro,
  o mais recente nem desempata arbitrariamente.
- "Último" (só quando **o texto** diz "último/última", "acabei de", "mais recente", "agora há
  pouco"): a transação registrada mais recentemente (`created_at`), do tipo citado ("último
  gasto" = EXPENSE, "última entrada" = INCOME, "último lançamento" = ambos). `last=true` da IA
  sem o marcador no texto é ignorado. "Desfaça a entrada" **não** é "o último lançamento".
- **Tipo sozinho não identifica alvo de remoção**: "desfaça a entrada", "apague o gasto" →
  zero mutação e "Qual entrada/gasto você quer remover?", mesmo com um único candidato. Tipo +
  valor ("a entrada de 50"), tipo + data ("a entrada de ontem") ou "último" explícito + tipo
  ("a última entrada") podem resolver (0/1/N continua valendo).
- **Ordem explícita de remover/editar nunca vira lançamento novo**: verbos inequívocos no
  texto ("remover", "apagar", "excluir", "deletar", "desfazer"; "cancelar", "tirar",
  "retirar", "estornar" só seguidos de lançamento/gasto/despesa/entrada/receita/registro/transação
  — "tira-gosto", "retirei 200 no caixa" não contam;
  "editar", "alterar", "mudar/trocar o valor/data/categoria") + proposta `CREATE` da IA →
  nada é criado (`TARGET_UNCLEAR` / `CORRECTION_NOT_CREATED`).
- **Data que identifica o lançamento é critério**: em "o gasto de ontem era 40" o dia ligado ao
  lançamento ("gasto/entrada/o ... de ontem", "do dia 07/10") precisa constar nos critérios do
  alvo; se a IA o usar só como data nova → `TARGET_UNCLEAR`, nada muda. Com cópula ("o gasto de
  30 era de ontem") a data é valor novo.
- Seleção e execução são serializadas por usuário: no commit da mensagem o Django trava a
  linha da mensagem e a do remetente, revalida pendência/contexto, seleciona os candidatos com
  `SELECT … FOR UPDATE`, valida 0/1/N e executa — tudo no mesmo `transaction.atomic()`.
- Remoção individual = soft delete (`soft_delete_transaction`, gera `DELETE` com `origin`).
- Vários de propósito ("apague todas as entradas de outubro"): o Django resolve o conjunto
  (máx. 50), responde quantidade e total e grava uma `PendingAction` (10 min) com os ids
  exatos, os critérios aprovados e uma impressão (SHA-256) dos campos de cada candidato (dono,
  tipo, valor, categoria, subcategoria, descrição, data). Só um "sim" (mensagem inteira) que
  seja **a mensagem seguinte** do remetente ao pedido executa — `soft_delete_transactions`, tudo
  ou nada, revalidando ids, dono, estado, impressão, critérios e total; qualquer diferença (até
  valores trocados entre dois itens mantendo o total) → nada é removido ("os lançamentos
  mudaram"). "Não", expiração ou **qualquer outra mensagem autenticada do remetente** (inclusive
  uma que falhou: IA fora do ar, áudio/imagem inválidos, ajuda, não entendida) invalida a
  pendência no momento em que é recebida. "Sim" sem pendência válida não faz nada. Edição em
  massa não é suportada.
- Correção ("errado, foram 80", "desculpa, foram 80", "não, foram 80", "foi 80, não 100"):
  **nunca cria** transação. Sem critérios, edita só o efeito da **mensagem imediatamente
  anterior** do remetente: ela precisa ter criado/editado um lançamento (PROCESSED, dentro do TTL
  do contexto), cuja última alteração ainda seja a daquela mensagem (não desfeita nem editada
  depois), ativo e do mesmo tipo citado; o valor novo precisa estar no texto e um segundo valor
  ("não 100") precisa ser o valor atual. Qualquer mensagem no meio (ajuda, consulta, falha,
  mídia inválida, outro pedido) quebra o vínculo → "Qual lançamento você quer corrigir?".
- Mensagem elíptica (só um valor, "80", "ok, 80") depois de um registro/edição **recente**
  (dentro do TTL do contexto) não vira lançamento novo (`CORRECTION_NOT_CREATED`, pergunta qual
  lançamento corrigir) — também quando o vínculo foi interrompido por outra mensagem (ajuda,
  consulta, falha de IA, mídia inválida): interrompido significa "não sabemos qual corrigir",
  não "criar". Sem mutação recente (ou após o TTL), segue o fluxo normal.
- **Datas do texto são valores** (`hints.temporal`, auditoria final P1#1): hoje/ontem/
  anteontem, dd/mm (ocorrência mais recente ≤ hoje), dd/mm/aaaa, "dia N" (regra da §4),
  "N de <mês> [de aaaa]", mês por extenso (mês mais recente já iniciado) ou "<mês> de aaaa",
  dia da semana sem qualificação (ocorrência mais recente ≤ hoje; "segunda" solta só com
  "na/de/do..."), esse/este mês, mês passado, essa semana / semana passada (intervalo que
  cobre as convenções seg–dom e dom–sáb), esse ano / ano passado. Em UPDATE/UNDO o alvo da IA
  precisa caber em uma dessas datas/períodos, cada data do texto precisa ser o alvo ou a data
  nova, e a data ligada ao lançamento ("a entrada do dia 5") precisa ser o alvo. Expressão de
  data não resolvível com segurança ("sexta passada", "dia 45", "31/02", "todo mês") →
  `TARGET_UNCLEAR`, zero mutação.
- **UPDATE só seleciona por critério do texto** (auditoria final P1#2): exige "último" ou um
  critério além do tipo comprovado pelo texto **como alvo** (valor escrito que não seja só o
  valor novo, data compatível, item do catálogo citado que não seja só a categoria nova,
  palavra descritiva da descrição presente no texto e diferente da descrição nova). Critério
  só proposto pela IA nunca é o motivo da seleção; com "último", um filtro extra não
  comprovado → `TARGET_UNCLEAR`. Tipo sozinho segue apenas o caminho contextual seguro.

### Política de desfazer (V1.1; revista em 2026-10-15)

- Desfazer a **última ação** (pilha) só quando o texto diz isso explicitamente: "desfaz a
  última alteração/ação/edição". Desfaz a última ação reversível ainda não desfeita do próprio
  usuário: `CREATE` (→ soft delete) ou `UPDATE` (→ restaura os valores anteriores exatos).
  "desfazer" sozinho **não** desfaz nada (pede identificação). Com tipo, valor, data,
  categoria, "último" ou "todos", vale a seleção acima.
- É uma pilha: criar 10 → mudar para 15 → "desfaz a última alteração" (volta a 10) → "desfaz a
  última alteração" (remove o registro) → idem ("nada para desfazer").
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
- Criação pelo canal: o dono é **sempre** o remetente. Nome de membro citado na mensagem ("curso da
  Ana", "presente pra Ana") é beneficiário/descrição, nunca dono. Pedido explícito de lançar para
  outra pessoa ("registra 100 para a Ana", "Ana gastou 100") é recusado, não gravado para ninguém
  (`AI_CONTRACT.md §5`, item 2).
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
