# Deploy e ambientes

Configuração de banco/Supabase: [`DATABASE.md §3`](DATABASE.md). Requisitos de segurança:
[`SECURITY.md`](SECURITY.md). Runbook operacional (health/readiness, recuperação manual,
retenção): [`OPERATIONS.md`](OPERATIONS.md).

> Implementado: Dockerfile, `compose.yaml`, Gunicorn, liveness `/health/` e readiness `/ready/`,
> recuperação manual de mensagens. Pendente: Nginx/TLS e procedimento de produção (seções 4–5
> são o alvo). A aplicação **não** está em produção.

## 1. Versões

- Python 3.12 (imagem `python:3.12-slim`).
- Django 5.2 LTS; psycopg 3; Gunicorn 26.2 (série suportada upstream; 23.x deixou de receber
  correções de segurança). Versões exatas em `requirements.txt`.
- PostgreSQL 17 (versão do projeto Supabase); testes locais em `postgres:17-alpine`.
- Evolution API em versão fixada (tag explícita, nunca `latest`).

## 2. Ambientes

| Ambiente | Banco | WhatsApp | OpenAI |
|---|---|---|---|
| Local | Supabase (projeto de desenvolvimento) | mock / instância de teste | mock ou chave dev |
| Teste/CI | PostgreSQL em container | mock | mock |
| Produção | Supabase PostgreSQL | instância Evolution de produção | chave de produção |

Um projeto Supabase de staging é recomendado antes de produção.

## 3. Artefatos

- `Dockerfile`: `python:3.12-slim`, usuário não-root, `requirements.txt` fixado,
  `collectstatic` no build (valores placeholder só para esse passo), `HEALTHCHECK` em
  `/health/` (liveness), `CMD` Gunicorn (`GUNICORN_WORKERS`, `GUNICORN_TIMEOUT`, workers `sync`,
  `--no-control-socket`: a interface de controle `gunicornc`, ativa por padrão desde a 25.1, não
  é usada).
- `compose.yaml`: serviço `web` (runserver, código montado, `.env`) apontando para o Supabase;
  serviço `test-db` só no profile `test`. Não há PostgreSQL local para a aplicação, Redis nem Nginx.
- `requirements.txt` (runtime) e `requirements-dev.txt` (testes/lint), versões fixadas.

Comandos locais:

```bash
docker compose up --build web                 # http://localhost:8000/admin/
python manage.py migrate                       # aplica migrations no banco DB_*
python manage.py seed_categories               # catálogo inicial (idempotente)
python manage.py createsuperuser
```

Sem Nginx, estáticos do Admin só são servidos com `DJANGO_DEBUG=True` (desenvolvimento).
O health check do container usa `Host: 127.0.0.1`: mantenha `127.0.0.1` em `DJANGO_ALLOWED_HOSTS`.

## 4. Runtime

```
Internet ──HTTPS──> Nginx ──HTTP──> Gunicorn (Django) ──SSL──> Supabase PostgreSQL
                      │                    └──HTTPS──> OpenAI / Evolution API
                      └── /static/ servido pelo Nginx
```

- **Gunicorn**: workers síncronos; `--timeout` maior que o pior caso do pipeline síncrono
  (somar download, transcrição/interpretação ou visão, envio Evolution e margem). Com os
  valores do `.env.example`, o teto externo chega a cerca de 70 s; o default é 120 s. Número de
  workers ajustado à memória/CPU do host, considerando a imagem decodificada e sua cópia base64.
- **Nginx**: TLS, redirect HTTP→HTTPS, `client_max_body_size` limitado, rate limit no
  webhook, `proxy_set_header X-Forwarded-Proto`, servir estáticos.
- Logs da aplicação em stdout/stderr (JSON ou texto estruturado), seguindo `SECURITY.md §5`;
  objetivo de retenção de 30 dias no host (rotação do driver de logs), ver `OPERATIONS.md §3`.
- Liveness `/health/` (sem banco) e readiness `/ready/` (`SELECT 1` com timeout curto, `503` se o
  banco não responde), sem expor dados nem versões; detalhes em `OPERATIONS.md §1`.

## 5. Procedimento de deploy

1. Build da imagem a partir de commit taggeado.
2. `python manage.py migrate` como etapa explícita (não no boot de cada worker).
3. `python manage.py check --deploy` sem avisos críticos.
4. Subir nova versão; validar `/health/` e `/ready/` (200) antes de liberar tráfego.
5. Confirmar webhook da Evolution apontando para a URL HTTPS com segredo correto.
6. Configurar `DJANGO_ALLOWED_HOSTS` com o domínio público e `127.0.0.1` para o health check;
   `host.docker.internal` é apenas para a integração Docker local quando necessária.

Mensagens interrompidas por restart/deploy: recuperação manual pelo comando
`inbound_recovery` (`OPERATIONS.md §2`); nunca SQL manual.

Rollback: voltar imagem anterior; migrations devem ser compatíveis com a versão anterior
sempre que possível (expandir → migrar → contrair).

## 6. Variáveis de ambiente

Lista canônica em [`.env.example`](../.env.example). Toda variável nova deve ser adicionada
lá com placeholder e comentário. Agrupamentos:

- Django: `DJANGO_ENV`, `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`,
  `DJANGO_CSRF_TRUSTED_ORIGINS`, `DJANGO_ADMIN_URL`, `TIME_ZONE`, `LOG_LEVEL`.
- Banco: `DB_*` (inclui `DB_ENABLE_RLS`, `DB_CONNECT_TIMEOUT`). Testes: `TEST_DB_*`.
- Gunicorn: `GUNICORN_WORKERS`, `GUNICORN_TIMEOUT`.
- OpenAI: `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_TIMEOUT_SECONDS`, `OPENAI_REASONING_EFFORT` (opcional)
  (+ modelo de transcrição na V1.3).
- Evolution: `EVOLUTION_API_BASE_URL`, `EVOLUTION_API_KEY`, `EVOLUTION_INSTANCE_NAME`,
  `EVOLUTION_WEBHOOK_SECRET`, `EVOLUTION_TIMEOUT_SECONDS`.
- Imagens: `OPENAI_VISION_MODEL`, `OPENAI_VISION_TIMEOUT_SECONDS`, `IMAGE_MAX_BYTES`,
  `IMAGE_MAX_PIXELS`. Sem Storage: `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY`/
  `SUPABASE_STORAGE_BUCKET` não são mais usadas (não configurar).
- Domínio: `TRANSACTION_MAX_AMOUNT`.

## 7. Pendências de infraestrutura

Não bloqueiam o desenvolvimento, mas precisam de decisão antes da produção:

- Host de produção (VPS, PaaS) e onde a Evolution API roda.
- Domínio e emissão de certificado TLS.
- Plano Supabase (backups/retenção, pooler).
