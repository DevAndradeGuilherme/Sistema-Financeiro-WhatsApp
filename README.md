# Sistema Financeiro WhatsApp

Controle financeiro pessoal e familiar operado pelo WhatsApp.

O usuário envia mensagens em linguagem natural — "10 reais de gasolina",
"recebi 500 dia 2", "quanto gastei de transporte esse mês?" — e o sistema registra
despesas/receitas ou responde consultas, sempre com validação, autorização e cálculo
feitos no servidor.

> **Status:** V1.5 concluída e validada em DEV — texto, áudio, comprovantes, famílias e
> analytics implementados. Preparação de produção (Nginx/TLS, host e deploy) ainda pendente.
> Veja `docs/ROADMAP.md`.

## Princípio

```
IA interpreta.  Django valida e autoriza.  PostgreSQL calcula e persiste.  WhatsApp comunica.
```

## Stack

| Camada | Tecnologia |
|---|---|
| Aplicação | Python, Django (monólito modular) |
| Banco | PostgreSQL gerenciado pelo Supabase |
| Mídias | Supabase Storage privado |
| Canal | WhatsApp via Evolution API |
| Interpretação | OpenAI (respostas estruturadas) |
| Execução | Docker, Gunicorn, Nginx |
| Administração | Django Admin |

## Documentação

| Documento | Conteúdo |
|---|---|
| [`AGENTS.md`](AGENTS.md) | Guia curto para coding agents; ponto de entrada |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Camadas, módulos, fronteiras, fluxos, decisões |
| [`docs/DOMAIN.md`](docs/DOMAIN.md) | Modelo conceitual, regras de negócio, autorização |
| [`docs/DATABASE.md`](docs/DATABASE.md) | Tabelas, tipos, constraints, Supabase |
| [`docs/AI_CONTRACT.md`](docs/AI_CONTRACT.md) | Intenções, schema estruturado, regras para a IA |
| [`docs/WHATSAPP.md`](docs/WHATSAPP.md) | Webhook Evolution, idempotência, envio, mídia |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Invariantes de segurança e práticas |
| [`docs/TESTING.md`](docs/TESTING.md) | Estratégia e convenções de testes |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Ambientes, Docker, Gunicorn, Nginx, variáveis |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Versões V1.0 a V1.5 |
| [`docs/CHANGELOG.md`](docs/CHANGELOG.md) | Histórico de mudanças |

## Configuração

Copie `.env.example` para `.env` e preencha os valores. Nunca versione `.env`.

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
python manage.py migrate && python manage.py seed_categories
python manage.py createsuperuser && python manage.py runserver
```

Testes e lint: `docs/TESTING.md`. Docker: `docs/DEPLOYMENT.md`.
