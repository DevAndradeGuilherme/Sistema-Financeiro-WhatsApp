# Auditoria técnica V1.0–V1.2

Data: 2026-10-07.

## Escopo

Revisão adversarial das versões V1.0, V1.1 e V1.2: autorização familiar, snapshot
histórico, idempotência, dinheiro, update/undo, consultas, contexto, OpenAI, Evolution,
telefonia, Admin, catálogo, banco, RLS, segredos, logs, falhas parciais, testes e dependências.

## Achados e correções

- **MEDIUM — família sem ADMIN pelo Admin/service:** era possível criar uma família vazia,
  adicionar somente MEMBER ou remover isoladamente o último ADMIN. O service e os formulários
  do Admin agora exigem exatamente um ADMIN; quatro testes de regressão cobrem esses caminhos.
- **LOW — notação científica em string monetária:** valores como `"1e3"` eram aceitos apesar
  do contrato decimal. A conversão agora rejeita expoentes; dois casos foram adicionados ao teste
  parametrizado.
- **CRITICAL/HIGH:** nenhum achado comprovado.

## Riscos aceitos e pendências

- **INFO — recuperação operacional:** uma interrupção abrupta antes do bloco atômico pode
  deixar mensagem `RECEIVED`/`READY` para
  recuperação manual; não há efeito financeiro a duplicar nessa janela.
- **INFO — ciclo de suporte:** Gunicorn 23.0.0 está fora das versões atualmente suportadas
  pelo upstream, sem advisory
  publicado identificado nesta auditoria. Upgrade amplo ficou para uma tarefa de compatibilidade,
  coerente com o estado ainda não pronto para produção.
- `MEMBER_LIVE_VALIDATION = PENDING_LIVE_VALIDATION` permanece pendente e não foi tratado como bug.
- Nenhuma migration ou alteração de RLS foi necessária.
