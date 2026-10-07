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
| S10 | Dados financeiros e mídias são sensíveis | acesso mínimo, buckets privados, retenção definida |

## 2. Identidade e autorização

- No canal WhatsApp, identidade = telefone do remetente extraído do payload **autenticado**
  (webhook com segredo válido). Nenhum outro campo identifica o usuário.
- Toda função de service/selector exposta ao canal recebe `actor: User` e aplica política.
  Não existe selector "sem ator" acessível pelo pipeline.
- Negações não revelam existência de outros usuários/transações.
- Django Admin: apenas staff; usar permissões do Django; superusers limitados ao necessário.
  Admin em produção atrás de HTTPS; considerar restrição por IP/URL não padrão.

## 3. IA

- Prompt injection é esperado: a mensagem do usuário pode tentar mudar instruções.
  Defesa principal é arquitetural (S1, validação Django, ausência de ferramentas com efeito).
- Enviar à OpenAI só o mínimo necessário (ver `AI_CONTRACT.md §2`).
- Nunca colocar segredos, IDs internos ou dados de terceiros no prompt.

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
- `service_role` key só no servidor, só quando necessária (Storage, V1.4).
- Buckets de mídia **privados**; acesso via URLs assinadas de curta duração, geradas
  somente após autorização no Django.
- Conexão com o banco via SSL.

## 7. Webhook e rede

Implementado no app `whatsapp`: segredo verificado antes do parse; `fromMe`, grupos e
broadcast ignorados; remetente desconhecido não é criado nem recebe resposta; payload bruto
(inclui `apikey` da instância enviada pela Evolution) não é persistido; logs só com
`provider_message_id`, instância, tipo e status — nunca texto, telefone, API key ou segredo;
`WHATSAPP_DEV_ECHO` proibido em produção; chamadas à Evolution sempre com timeout.

- HTTPS obrigatório (Nginx com TLS).
- Limite de tamanho de corpo no Nginx e no Django.
- Rate limiting básico no Nginx para o endpoint de webhook.
- Timeouts em todas as chamadas de saída (OpenAI, Evolution, Supabase).

## 8. Dados

- Soft delete para transações; exclusão física apenas por processo administrativo documentado.
- Retenção de payload bruto e mídias deve ser definida antes de armazená-los.
- Dumps de banco nunca no repositório nem em máquinas sem criptografia.

## 9. Checklist de revisão de segurança

- [ ] Algum campo da IA ou do payload influencia autorização? (não pode)
- [ ] Algum selector/serviço novo é acessível sem `actor`?
- [ ] ADMIN consegue editar/desfazer transação de terceiro por algum caminho?
- [ ] Idempotência garantida por constraint, não só por `if exists`?
- [ ] Algum `float` em dinheiro?
- [ ] Algum segredo, telefone completo ou payload bruto em log?
- [ ] Novas variáveis de ambiente adicionadas ao `.env.example` com placeholder?
- [ ] Timeouts configurados em chamadas externas?
