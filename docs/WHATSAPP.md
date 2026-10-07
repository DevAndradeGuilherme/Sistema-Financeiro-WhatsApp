# WhatsApp (Evolution API)

Integração do canal. Fluxo geral do pipeline: [`ARCHITECTURE.md §4`](ARCHITECTURE.md).
Tabela `InboundMessage`: [`DATABASE.md`](DATABASE.md).

## 1. Responsabilidades do app `whatsapp`

- Receber e autenticar webhooks da Evolution API.
- Normalizar payload em uma estrutura interna (não espalhar o formato da Evolution pelo código).
- Garantir idempotência.
- Identificar o remetente (telefone → `User`).
- Orquestrar: texto → `ai` → validação → `transactions` → resposta.
- Enviar respostas pela Evolution API.
- (V1.3/V1.4) Baixar mídias e convertê-las em texto/proposta.

O app não contém regra financeira; chama services/selectors dos apps de domínio.

## 2. Webhook

- Endpoint dedicado, `POST` apenas, `csrf_exempt` (é server-to-server), sem sessão.
- **Autenticação obrigatória**: segredo compartilhado `EVOLUTION_WEBHOOK_SECRET`, enviado
  em header customizado configurado no webhook da Evolution; se a versão usada não suportar
  headers, segredo como segmento do path. Comparação com `hmac.compare_digest`.
  Requisição sem segredo válido → `401`/`403`, sem processamento e sem log do segredo.
- Validar também que `instance` corresponde a `EVOLUTION_INSTANCE_NAME`.
- Limitar tamanho do corpo e rejeitar JSON inválido com `400`.
- Eventos de interesse: mensagens recebidas (`messages.upsert`). Demais eventos → `200` no-op.
- Ignorar (`200`, status `IGNORED` quando registrado):
  - mensagens com `fromMe = true` (inclui as próprias respostas do sistema);
  - mensagens de grupos e broadcast/status;
  - tipos não suportados na versão atual (responder orientação curta, se for usuário conhecido).
- Responder `200` sempre que a mensagem foi aceita ou deliberadamente ignorada, para evitar
  reentregas desnecessárias. Erros internos após a persistência não devem gerar reprocessamento
  duplicado (idempotência cobre).

Os nomes exatos de campos do payload dependem da versão da Evolution API em uso: fixar a
versão no deploy e manter fixtures reais anonimizadas em testes.

## 3. Idempotência

Duas camadas, ambas garantidas por constraint no banco:

1. `InboundMessage`: `UNIQUE (instance, external_message_id)`. O webhook insere a mensagem
   **antes** de qualquer processamento. `IntegrityError` → duplicata → `200` no-op, sem
   chamar IA e sem responder de novo.
2. `Transaction.idempotency_key = "whatsapp:<instance>:<external_message_id>"` com `UNIQUE`.
   Protege contra corrida ou reprocessamento manual.

Mensagens que ficarem em `PROCESSING`/`FAILED` (ex.: crash) não são reprocessadas
automaticamente no MVP; reprocessamento é ação manual do administrador e continua protegido
pela camada 2.

## 4. Identificação do remetente

- Extrair o número do JID do remetente (`<numero>@s.whatsapp.net`) e normalizar para E.164
  em `common.phone`.
- **Atenção a números brasileiros**: o JID pode vir sem o nono dígito de celular
  (ex.: `553199998888` em vez de `5531999998888`). A normalização deve considerar
  ambas as formas ao buscar o usuário e cadastrar o formato canônico com nono dígito.
- Versões recentes do WhatsApp podem identificar contatos por LID (`@lid`) em vez do número.
  Se o payload não trouxer o telefone, a mensagem não é associada a usuário (status `IGNORED`)
  até existir regra documentada para mapear LID.
- Número desconhecido ou usuário inativo: registrar, não chamar a IA, não criar transação,
  não revelar informações. Sem resposta automática no MVP.

## 5. Envio de respostas

- Cliente Evolution encapsulado em `apps/whatsapp/` (ex.: `client.py`), com timeout
  (`EVOLUTION_TIMEOUT_SECONDS`) e `EVOLUTION_API_KEY` em header; nunca logar a chave.
- Mensagens curtas, pt-BR, valores em `R$ 1.234,56`, datas `dd/mm/aaaa`.
- Confirmação de criação inclui: tipo, valor, categoria/subcategoria, data e como desfazer
  (quando disponível, V1.1).
- Falha no envio: registrar erro; não desfazer a transação já criada.
- Textos de resposta centralizados (ex.: `apps/whatsapp/messages.py`), não espalhados.

## 6. Mídia (versões futuras)

### Áudio — V1.3

`message_type=AUDIO` → download da mídia pela Evolution → transcrição (OpenAI) →
`InboundMessage.text` = transcrição → mesmo pipeline do texto, com `source=AUDIO`.
Limites de duração/tamanho configuráveis. Arquivo de áudio não precisa ser persistido
após a transcrição, salvo decisão documentada.

### Imagem — V1.4

`message_type=IMAGE` → download → upload em bucket **privado** do Supabase Storage
(caminho sem dados pessoais legíveis) → extração multimodal estruturada → mesma validação
→ confirmação com o usuário → `create_transaction` com `source=IMAGE`.
O fluxo de confirmação exato será especificado na V1.4.

Toda mídia é entrada não confiável: validar MIME/tamanho, nunca executar nem renderizar.
