# Auditoria técnica V1.3–V1.5

Data: 2026-10-07. Segunda auditoria formal, com regressão dirigida de V1.0–V1.2.

## Escopo e baseline

Áudio/transcrição, imagem/Pillow/Storage/Vision, `ReceiptAttachment`, analytics, contexto,
webhook/idempotência, autorização familiar, dinheiro/datas, Admin, migrations, RLS, segredos,
logs, dependências, falhas parciais e recursos síncronos. Baseline limpo no commit `d49e0fe`:
768 testes PostgreSQL passando; Ruff, Django check e migrations check sem erro.

DEV foi consultado somente para validação: RLS 19/19, zero policy pública, migrations V1.4/V1.5
aplicadas, bucket existente e privado. O caso fictício soft-deleted permaneceu excluído do ledger.

## Achados e correções

- **MEDIUM — resposta Evolution sem teto em caminhos secundários** (`whatsapp/client.py`).
  Respostas JSON não-mídia usavam `read()` e corpos HTTP de erro também eram ilimitados. Uma
  Evolution defeituosa (probabilidade baixa) podia ocupar a memória do worker (impacto alto de
  disponibilidade); o teste existente cobria apenas sucesso de download. Correção: teto de
  1 MiB nas respostas comuns e 4 KiB nos erros, preservando o teto específico da mídia.
- **MEDIUM — precedência/contexto de `MONTHLY_TREND`** (`whatsapp/actions.py`). `start_date` era
  validada embora não participasse do cálculo; uma data contraditória rejeitava a consulta e a
  janela de um dia observada ao vivo era persistida. Como campos extras já ocorreram ao vivo,
  a probabilidade era média e o impacto era resposta/follow-up semanticamente errado. Os testes
  não combinavam `months` com datas inconsistentes. Correção: `months` define a largura,
  `end_date` só ancora o mês final e o contexto guarda a janela efetiva.
- **MEDIUM — timeout Gunicorn menor que o pior caminho síncrono** (`Dockerfile`, `.env.example`).
  O default de 60 s era menor que aproximadamente 70 s de timeouts externos sequenciais de
  áudio/imagem. Sob latência alta (probabilidade média), o worker podia ser encerrado antes dos
  próprios clientes (impacto de disponibilidade). Não havia teste cruzando os timeouts. Correção:
  default e exemplo em 120 s, com dimensionamento documentado.
- **LOW — timeout Storage não positivo** (`common/storage.py`). Zero/negativo passava pela
  configuração (baixa probabilidade, falha operacional); os testes só cobriam ausente/inseguro.
  Agora falha explicitamente, como Evolution/OpenAI.
- **LOW — evento de log incorreto** (`whatsapp/audio.py`). Toda rejeição de imagem era registrada
  como `audio.rejected` (baixo impacto, diagnóstico enganoso) e os testes não verificavam o evento.
  Agora usa `media.rejected` com o tipo técnico da mensagem.
- **LOW — README defasado.** Declarava V1.0 em andamento e mídia futura (impacto documental);
  foi alinhado ao estado V1.5 e às pendências reais de preparação.
- **MEDIUM — falha ao persistir `ReceiptAttachment` após upload** (`whatsapp/image.py`). Uma
  `IntegrityError` deixava a mensagem em `RECEIVED` sem resposta, embora o objeto fosse removido.
  Agora a mensagem termina em `FAILED` com código sanitizado e resposta técnica; regressão cobre
  limpeza do Storage e ausência de Vision/transação.
- **CRITICAL/HIGH:** nenhum achado comprovado.

Foram adicionados casos de regressão para limites Evolution em sucesso/erro, timeout Storage
zero/negativo, precedência/contexto mensal, persistência de attachment e log de imagem.

Validação final: **774 testes passando** em PostgreSQL; Ruff check/format, Django check,
`makemigrations --check --dry-run`, `pip check` e `git diff --check` sem erro. Nenhuma migration.

## Confirmações

- Webhook autentica em tempo constante antes do body; filtros de identidade precedem mídia;
  unicidade no banco impede repetição sequencial/concorrente de texto, áudio e imagem.
- Áudio não persiste bytes, usa nome/tipo controlados, timeout e `max_retries=0`; só o transcript
  validado entra no domínio textual.
- A detecção local de áudio usa assinaturas de contêiner; duração declarada permanece apenas dica
  não confiável e payloads malformados dependem da rejeição do transcritor. Não foi demonstrado
  bypass financeiro; parser local completo para todos os formatos não existe.
- Imagem é detectada pelos bytes, limitada, verificada e regravada após orientação EXIF; somente
  a versão normalizada, sem metadados, vai ao Storage privado e à Vision.
- Vision usa uma Responses API call, schema estrito, `store=False` e validação final Django.
- `ReceiptAttachment` é um-para-um, metadados somente, Admin read-only e `PROTECT` para transação.
- Analytics usam `ReadScope`, `Transaction.active()`, SQL/`Decimal`; `TransactionChange` e source
  não alteram o ledger. Percentuais e arredondamentos conferem com o contrato.
- FAMILY/FAMILY_MEMBER revalidam o ADMIN e o membership em cada mensagem; contexto não autoriza.
- CREATE/UPDATE/UNDO, TOTAL/BALANCE/TOP/BREAKDOWN/LIST/COMPARE, fallback, telefone e Admin não
  apresentaram regressões V1.0–V1.2.
- Django 5.2.18 e Pillow 12.3.0 incorporam as correções de segurança correntes consultadas;
  OpenAI 2.54.0 não tem advisory publicado identificado. Gunicorn 23 é a exceção de suporte.

## Riscos e classificação para produção

### BLOCKER

- Antes de entrar em produção: concluir infraestrutura já fora deste escopo (host/domínio,
  Nginx/TLS, configuração e procedimento de deploy). Não há blocker de código nas versões auditadas.

### SHOULD_FIX_BEFORE_PROD

- Decidir e implementar a política de retenção de imagens/anexos e mensagens/transcripts.
- Migrar do Gunicorn 23, sem suporte upstream, para uma versão suportada após teste de compatibilidade.
- Definir memória/workers, rate limit e timeouts do proxy; 40 milhões de pixels podem consumir
  memória significativa por worker durante decodificação e base64.
- Documentar a recuperação manual/observabilidade de mensagens `RECEIVED`/`READY` interrompidas.

### ACCEPTABLE_FOR_INITIAL_PROD

- Processamento síncrono para cerca de 15 pessoas, com os limites, workers e rate limit acima.
- Duração de áudio baseada no metadado não confiável; tamanho real e timeout continuam limitados.
  A checagem local identifica o container por assinatura; a transcrição rejeita payload inválido.
- Resíduo operacional de Storage/attachments pode existir em falhas fora dos caminhos cobertos;
  o caminho `IntegrityError` após upload agora limpa o objeto e termina a mensagem sem efeito financeiro.
- `CATEGORY_TREND` compara mês atual parcial com mês anterior completo e explicita os períodos.
- `MEMBER_LIVE_VALIDATION` pendente, com matriz automatizada completa e rollout monitorado.

### POST_PROD

- Automatizar recuperação/expurgo após definir retenção e observar volume real.
- Reavaliar limites de imagem, workers e índice parcial de transações ativas com métricas reais.

## Pendências conhecidas e decisão

- `V1_5_LIVE_VALIDATION = COMPLETE`.
- `MEMBER_LIVE_VALIDATION = PENDING_LIVE_VALIDATION` — risco operacional, não bug.
- `RETENTION_POLICY = PENDING_PRODUCT_DECISION` — deve ser resolvida antes do primeiro uso real.
- `host.docker.internal` está claro para DEV em `WHATSAPP.md`; produção deve usar domínio explícito
  em `DJANGO_ALLOWED_HOSTS`, agora reforçado no checklist de preparação.
- `RECOMMEND_PRODUCTION_PREPARATION = YES`: é seguro iniciar a preparação, sem fazer deploy,
  desde que os itens BLOCKER/SHOULD_FIX_BEFORE_PROD sejam tratados antes do uso real.
