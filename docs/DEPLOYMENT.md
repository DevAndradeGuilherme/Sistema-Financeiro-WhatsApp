# Deploy e ambientes

Configuração de banco/Supabase: [`DATABASE.md §3`](DATABASE.md). Requisitos de segurança:
[`SECURITY.md`](SECURITY.md).

> Nada disto está implementado ainda. Este documento define o alvo para a V1.0.

## 1. Versões alvo

- Python 3.12+.
- Django 5.2 LTS (ou LTS vigente no início da V1.0).
- PostgreSQL na versão fornecida pelo Supabase; testes locais na mesma major.
- Evolution API em versão fixada (tag explícita, nunca `latest`).

## 2. Ambientes

| Ambiente | Banco | WhatsApp | OpenAI |
|---|---|---|---|
| Local | PostgreSQL em container | mock / instância de teste | mock ou chave dev |
| Teste/CI | PostgreSQL em container | mock | mock |
| Produção | Supabase PostgreSQL | instância Evolution de produção | chave de produção |

Um projeto Supabase de staging é recomendado antes de produção.

## 3. Artefatos previstos (V1.0)

- `Dockerfile`: imagem Python slim, usuário não-root, dependências fixadas, `collectstatic`
  no build, entrypoint com Gunicorn.
- `docker-compose.yml` (local): app + PostgreSQL (+ Evolution opcional).
- Arquivo de dependências com versões fixadas.

## 4. Runtime

```
Internet ──HTTPS──> Nginx ──HTTP──> Gunicorn (Django) ──SSL──> Supabase PostgreSQL
                      │                    └──HTTPS──> OpenAI / Evolution API
                      └── /static/ servido pelo Nginx
```

- **Gunicorn**: workers síncronos; `--timeout` maior que o pior caso do pipeline síncrono
  (`OPENAI_TIMEOUT_SECONDS` + `EVOLUTION_TIMEOUT_SECONDS` + margem). Número de workers
  ajustado à memória/CPU do host.
- **Nginx**: TLS, redirect HTTP→HTTPS, `client_max_body_size` limitado, rate limit no
  webhook, `proxy_set_header X-Forwarded-Proto`, servir estáticos.
- Logs da aplicação em stdout/stderr (JSON ou texto estruturado), seguindo `SECURITY.md §5`.
- Health check simples (`/health/`) sem expor dados nem versões.

## 5. Procedimento de deploy

1. Build da imagem a partir de commit taggeado.
2. `python manage.py migrate` como etapa explícita (não no boot de cada worker).
3. `python manage.py check --deploy` sem avisos críticos.
4. Subir nova versão; validar health check.
5. Confirmar webhook da Evolution apontando para a URL HTTPS com segredo correto.

Rollback: voltar imagem anterior; migrations devem ser compatíveis com a versão anterior
sempre que possível (expandir → migrar → contrair).

## 6. Variáveis de ambiente

Lista canônica em [`.env.example`](../.env.example). Toda variável nova deve ser adicionada
lá com placeholder e comentário. Agrupamentos:

- Django: `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `TIME_ZONE`.
- Banco: `DB_*`.
- OpenAI: `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_TIMEOUT_SECONDS`
  (+ modelo de transcrição na V1.3).
- Evolution: `EVOLUTION_API_BASE_URL`, `EVOLUTION_API_KEY`, `EVOLUTION_INSTANCE_NAME`,
  `EVOLUTION_WEBHOOK_SECRET`, `EVOLUTION_TIMEOUT_SECONDS`.
- Supabase Storage (V1.4): `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_STORAGE_BUCKET`.
- Domínio: `TRANSACTION_MAX_AMOUNT`.

## 7. Pendências de infraestrutura

Não bloqueiam o desenvolvimento, mas precisam de decisão antes da produção:

- Host de produção (VPS, PaaS) e onde a Evolution API roda.
- Domínio e emissão de certificado TLS.
- Plano Supabase (backups/retenção, pooler).
