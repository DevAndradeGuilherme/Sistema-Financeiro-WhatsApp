# Segurança

Invariantes e práticas obrigatórias. Regras de autorização detalhadas: [`DOMAIN.md §6`](DOMAIN.md).
Revisores (Codex) devem usar este documento como checklist.

## 1. Invariantes

| # | Invariante | Onde é garantido |
|---|---|---|
| S1 | Autorização nunca depende da IA | `policies.py` usando o ator; campos da IA são só pistas |
| S2 | Usuário comum acessa somente os próprios dados | selectors com escopo obrigatório |
| S3 | Family ADMIN apenas **lê** dados de terceiros da sua família | `can_modify_transaction` = somente owner |
| S4 | Segredos nunca são versionados | `.gitignore`, `.env.example` só com placeholders |
| S5 | Webhook exige autenticação | header `X-Webhook-Secret` + `compare_digest` (`WHATSAPP.md §3`) |
| S6 | Mensagens duplicadas são idempotentes | constraints `UNIQUE` (ver `WHATSAPP.md §5`) |
| S7 | Logs não expõem segredos nem dados financeiros completos | redaction em `common` |
| S8 | Soft delete onde o domínio define | `deleted_at`; selectors filtram |
| S9 | Toda entrada externa é não confiável | payload Evolution, saída da IA, mídias, Admin inputs |
| S10 | Dados financeiros e mídias são sensíveis | acesso mínimo, mídia não retida (`IMAGE_RETENTION = NONE`), retenção definida |

## 2. Identidade e autorização

- No canal WhatsApp, identidade = telefone do remetente extraído do payload **autenticado**
  (webhook com segredo válido). Nenhum outro campo identifica o usuário.
- Toda função de service/selector exposta ao canal recebe `actor: User` e aplica política.
  Não existe selector "sem ator" acessível pelo pipeline.
- Negações não revelam existência de outros usuários/transações.
- Django Admin: apenas staff; usar permissões do Django; superusers limitados ao necessário.
  Admin em produção atrás de HTTPS; considerar restrição por IP/URL não padrão.
- Family ADMIN/MEMBER são independentes de staff/superuser/permissões Django. Staff limitado
  não altera privilégios nem contas de superuser ou de outro staff (inclusive pelo endpoint de senha).
- Gestão familiar exige permissões explícitas: criar = `add_family` + `add_familymember`;
  adicionar/remover/transferir = `change_family` + respectivamente `add_familymember`,
  `delete_familymember`, `change_familymember`. Views usam `admin_site.admin_view`, CSRF e POST;
  GET não altera estado. Services de transferência/remoção revalidam o operador.
- Transferência registra no Django `LogEntry` o operador e ids do ADMIN anterior/destino,
  no mesmo atomic da mudança. Sem telefone, senha ou dados financeiros no texto do evento.
- `Transaction`, `TransactionChange`, `InboundMessage`, `ConversationContext` e `ReceiptAttachment`
  não aceitam add/change/delete nem ações financeiras pelo Admin, mesmo para superusers.
  Mensagens não exibem `text` (inclui transcrições); UserAdmin não mostra hashes de senha.
  Anexos mostram somente metadados: referências de Storage em seção LEGACY, sem links ou previews.
  O catálogo continua editável exclusivamente por superuser com as constraints existentes.

## 3. IA

- Prompt injection é esperado: a mensagem do usuário pode tentar mudar instruções.
  Defesa principal é arquitetural (S1, validação Django, ausência de ferramentas com efeito).
- Enviar à OpenAI só o mínimo necessário (ver `AI_CONTRACT.md §2`).
- Nunca colocar segredos, IDs internos ou dados de terceiros no prompt.
- Implementado: a IA recebe só texto, data/timezone e catálogo ativo; `store=False`; sem tools;
  contrato sem campos de usuário/escopo; o usuário consultado é sempre o remetente
  (`InboundMessage.user`); ids de categoria revalidados; totais só por selectors (SQL).
  Testes cobrem "quanto a Maria gastou?" e payload com ids manipulados.
- V1.1: a IA não envia id de transação (o schema estrito o rejeita); a transação a editar é
  buscada só entre as do remetente; editar/desfazer exigem ser o dono (`can_modify_transaction`);
  o contexto de follow-up é por usuário, expira e só contém parâmetros validados. Testes:
  "edita o gasto da Maria", "desfaz o gasto do João", contexto de outro usuário.
- V1.2: ADMIN da família = **somente leitura** sobre terceiros. `ReadScope` só é criado após
  `can_view_family` (papel atual, revalidado a cada mensagem); membro resolvido por nome apenas
  dentro da família, autorização antes da resolução; ambiguidade nunca escolhe; snapshot
  `Transaction.family` impede leitura retroativa; IA não recebe lista de membros, papel, ids
  ou telefones; negações logadas só com escopo e código (`query.scope_denied`).
- `OPENAI_API_KEY` nunca é logada nem persistida; logs da IA só têm métricas (modelo, intenção,
  duração, tokens).

## 4. Segredos e configuração

- Segredos apenas em variáveis de ambiente / secret store do host.
- `.env` nunca commitado. `.env.example` contém apenas placeholders.
- `DEBUG=False` em produção; `SECRET_KEY` forte e exclusiva por ambiente.
- Settings de produção: `SECURE_PROXY_SSL_HEADER`, `SESSION_COOKIE_SECURE`,
  `CSRF_COOKIE_SECURE`, `SECURE_HSTS_SECONDS`, `ALLOWED_HOSTS` e `CSRF_TRUSTED_ORIGINS` explícitos.
- Rotacionar imediatamente qualquer segredo exposto (commit, log, print, issue).
- Agents: nunca imprimir valores de `.env` em saídas, commits ou documentação.

## 5. Logs

- Logar: ids internos, status, intenção, códigos de erro, latência.
- Não logar: chaves/tokens/headers de autenticação, payload bruto completo, texto completo
  de mensagens em nível INFO, telefones completos (mascarar: `+55319****8888`).
- Exceções de clientes HTTP podem conter headers — sanitizar antes de logar.

## 6. Supabase

- Desabilitar exposição do schema da aplicação na Data API (PostgREST) e/ou habilitar RLS
  sem policies em todas as tabelas. Nenhum acesso a dados pelo client-side do Supabase.
- A aplicação **não usa** a `service_role` key (nem Storage) desde 2026-10-08: mídias não são
  armazenadas. Não configurá-la no runtime; se algum dia for necessária, só no servidor.
- Bucket legado de comprovantes (DEV) permanece privado até a limpeza manual (`DATABASE.md`).
- Conexão com o banco via SSL.

## 7. Webhook e rede

Implementado no app `whatsapp`: segredo verificado antes do parse; `fromMe`, grupos e
broadcast ignorados; remetente desconhecido não é criado nem recebe resposta; payload bruto
(inclui `apikey` da instância enviada pela Evolution) não é persistido; logs só com
`provider_message_id`, instância, tipo e status — nunca texto, telefone, API key ou segredo;
chamadas à Evolution sempre com timeout.

- HTTPS obrigatório (Nginx com TLS).
- Limite de tamanho de corpo no Nginx e no Django.
- Rate limiting básico no Nginx para o endpoint de webhook.
- Timeouts em todas as chamadas de saída (OpenAI, Evolution).

## 7.1 Áudio (V1.3)

- Mídia é entrada não confiável: MIME, extensão, `fileName`, tamanho e duração declarados nunca
  decidem nada sozinhos; formato vem dos bytes; nome enviado à OpenAI é nosso (`audio.<ext>`).
- Sem SSRF: download só pela Evolution, pelo id da mensagem; nenhuma URL do payload é acessada.
- Memória limitada: leitura da resposta com teto, base64 checado antes de decodificar, áudio
  descartado após a transcrição; nenhum arquivo temporário; nada de mídia no banco ou em logs.
- Filtros (fromMe, grupo, broadcast, remetente desconhecido/inativo) rodam antes do download.

## 7.2 Imagens / comprovantes (V1.4)

- Imagens de comprovante são processadas de forma efêmera, em memória, e **não são retidas**:
  sem Storage, arquivo temporário, bytes, base64, data URL, OCR/texto do comprovante, URL ou path persistidos.
  `IMAGE_RETENTION = NONE`.
- Formato pelos bytes + Pillow; limite de bytes (`IMAGE_MAX_BYTES`) e de pixels
  (`IMAGE_MAX_PIXELS`, checado no cabeçalho antes de decodificar; bomba de descompressão vira erro).
- EXIF/GPS e metadados de texto removidos na regravação; a OpenAI recebe **somente** a versão
  normalizada (nunca os bytes originais), `store=False`.
- Perfis ICC incorporados também são removidos explicitamente após aplicar a orientação:
  regravar PNG sem argumento `icc_profile` não basta, pois Pillow pode herdar esse campo.
- `ReceiptAttachment`: só metadados técnicos; Admin read-only, sem visualização de imagem.
- Logs técnicos apenas (id, bytes, formato, dimensões, modelo, tokens, códigos) — nunca imagem,
  base64, valores, descrição, CPF/CNPJ, chave Pix ou telefone.

## 8. Dados

- Soft delete para transações; exclusão física apenas por processo administrativo documentado.
- Retenção de payload bruto e mídias deve ser definida antes de armazená-los (hoje nenhum
  dos dois é armazenado). Retenção de `InboundMessage`, metadados e auditoria: pendente.
- Dumps de banco nunca no repositório nem em máquinas sem criptografia.

## 9. Checklist de revisão de segurança

- [ ] Algum campo da IA ou do payload influencia autorização? (não pode)
- [ ] Algum selector/serviço novo é acessível sem `actor`?
- [ ] ADMIN consegue editar/desfazer transação de terceiro por algum caminho?
- [ ] Alguma mutação sem registro em `TransactionChange` ou fora do `atomic` da mensagem?
- [ ] Alguma leitura de terceiro sem passar por `transactions.scopes` (ex.: filtro por `user_id` vindo de fora)?
- [ ] Alguma URL/caminho/nome de arquivo vindo de webhook usado para baixar ou gravar mídia?
- [ ] Alguma mídia (bytes/base64/arquivo/URL) persistida ou logada? (não pode)
- [ ] Idempotência garantida por constraint, não só por `if exists`?
- [ ] Algum `float` em dinheiro?
- [ ] Algum segredo, telefone completo ou payload bruto em log?
- [ ] Novas variáveis de ambiente adicionadas ao `.env.example` com placeholder?
- [ ] Timeouts configurados em chamadas externas?
