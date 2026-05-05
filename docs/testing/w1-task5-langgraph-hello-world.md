# W1 · Task 5 — LangGraph hello-world + dispatch reale

## Cosa è stato consegnato

Il primo grafo LangGraph **end-to-end**, in versione hello-world.

- Due nodi: **triage** (Claude Haiku 4.5, structured output Pydantic) → **publisher** (httpx → GitHub API).
- Il webhook handler ora **schedula il run del grafo come `BackgroundTask`** dopo la validazione e ritorna 202 immediato.
- Il publisher posta sulla PR del playground un commento del tipo `Hello from agent — triage classified this as **bugfix** (medium).` usando l'installation token costruito in Task 4.
- Il runner è composto in `lifespan` di FastAPI: una sola istanza di `httpx.AsyncClient`, una sola di `GitHubAppAuth` (cache token in memoria), una sola di `GitHubClient`. La webhook handler li legge da `request.app.state`.

**Cosa NON è ancora stato fatto:**
- Context Gatherer / Reviewer / Critic — quelli sono W2-W3.
- Diff fetching reale — il triage decide su `pull_request.title + body`, non sul diff.
- LangSmith tracing — Task 7.
- Persistenza in Postgres dei costi/run — Task 6 / W4.
- Job queue (ARQ/Celery) — W4 deploy. Per ora `BackgroundTasks` di FastAPI; le eccezioni nel task background vengono catturate e loggate via `structlog` ma non propagate al chiamante.

## Setup dell'ambiente di test

Ai requisiti di Task 4 (`GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_WEBHOOK_SECRET`) si aggiunge:

```bash
# In .env
ANTHROPIC_API_KEY=sk-ant-api03-...
```

Senza `ANTHROPIC_API_KEY` valida, il `lifespan` lascia `agent_runner = None` e il webhook **logga un warning** "agent runner not configured; skipping dispatch" senza dispatchare nulla — utile per testare la pipeline HTTP senza spendere token Anthropic.

## Scenari da testare manualmente

### 1 — Pytest verde

```bash
uv run pytest -v
```

Atteso: **38 passed**. I 10 nuovi sono:

- `tests/unit/test_node_triage.py` (3): contratto del nodo triage, mock chain.
- `tests/unit/test_node_publisher.py` (3): contratto publisher, fake client, fallback senza triage.
- `tests/unit/test_graph.py` (1): grafo end-to-end con mock LLM + recording client.
- `tests/unit/test_github_client.py` (2): chiamata `POST /comments` con installation token + 5xx → `GitHubAPIError`.
- `tests/unit/test_webhook_security.py` (1, nuovo): il webhook schedula il run dell'agente in background.

### 2 — Bruno collection verde

```bash
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  uv run uvicorn pr_review_agent.main:app --port 8001
# In altra shell
export GITHUB_WEBHOOK_SECRET=test-bruno-secret
cd bruno && npx --yes @usebruno/cli run --env local
```

Atteso: **12 request, 24/24 assert** (invariato). Il dispatch in background **non altera** la response sincrona del webhook: la collection vede gli stessi status code di Task 4. Per verificare che il dispatch parta davvero, vedi §4 e §5 sotto.

### 3 — Esecuzione del grafo isolata via REPL (no GitHub)

Utile per vedere come il triage classifica un titolo + body senza fare giri attorno al webhook. Non posta su nessuna PR, perché passiamo un `GitHubClient` recording.

```bash
uv run python -c '
import asyncio
from pr_review_agent.agent.graph import build_graph
from pr_review_agent.agent.nodes.triage import make_triage_node
from pr_review_agent.agent.nodes.publisher import make_publisher_node
from langchain_core.runnables import RunnableLambda
from pr_review_agent.agent.models import TriageDecision, ChangeType, RiskLevel

class Recorder:
    posted = []
    async def post_pr_comment(self, **kw): self.posted.append(kw)

decision = TriageDecision(change_type=ChangeType.docs, risk_level=RiskLevel.low)
graph = build_graph(
    triage=make_triage_node(RunnableLambda(lambda _: decision)),
    publisher=make_publisher_node(Recorder()),
)
state = {"repo":"x/y","pr_number":1,"pr_title":"Update README","pr_body":"","installation_id":7}
asyncio.run(graph.ainvoke(state))
'
```

Atteso: nessun output di errore. Il commento sarebbe stato `Hello from agent — triage classified this as **docs** (low).`.

### 4 — Test dispatch in background con mock LLM

Stesso flusso di §1 ma osservando il warning di `structlog` quando `ANTHROPIC_API_KEY` è vuoto:

```bash
unset ANTHROPIC_API_KEY
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  uv run uvicorn pr_review_agent.main:app --port 8001
```

Lancia una request firmata via Bruno (o `curl`) all'endpoint webhook con un payload `pull_request`. Atteso nei log uvicorn:

```
agent runner not configured; skipping dispatch repo=... pr_number=...
```

E response sincrona del webhook ancora `202 accepted` con `installation_id` corretto.

### 5 — End-to-end vero con ngrok + GitHub App reale

**Il momento di verità.** Prima volta che il bot commenta davvero su una PR.

1. Esporta tutte le credenziali reali nel tuo `.env`:
   ```
   GITHUB_APP_ID=<reale>
   GITHUB_APP_PRIVATE_KEY_PATH=/home/cesar/.secrets/pr-review-agent.pem
   GITHUB_WEBHOOK_SECRET=<reale, quello della tua App>
   ANTHROPIC_API_KEY=sk-ant-api03-...
   ```
2. Avvia uvicorn: `uv run uvicorn pr_review_agent.main:app --port 8001`.
3. In altra shell: `ngrok http --url=<tuo-dominio-statico> 8001`.
4. Aggiorna **Webhook URL** della GitHub App a `https://<tuo-dominio-statico>/webhook/github` se non l'avevi già fatto.
5. Sul repo `pr-review-agent-playground`, apri una PR (anche solo aggiungendo una riga al README su un branch). Suggerimento per un esito visibile: titolo `"Add docs"` + body `"Tiny doc tweak."` → il triage classifica come `docs` con `low`.
6. Aspetta qualche secondo. Atteso: nei commenti della PR compare `Hello from agent — triage classified this as **docs** (low).`. Nei log di uvicorn vedi una linea per la chiamata a `https://api.github.com/app/installations/.../access_tokens` (token exchange) e una per `https://api.github.com/repos/.../comments` (post commento).
7. Se vedi 401 sulla seconda chiamata: il webhook secret non coincide. Se vedi 5xx con `GitHubAPIError` dal background task: GitHub ha avuto un singhiozzo, il task non viene retried (W3 lo aggiunge).

## Cosa cercare nei log

- All'avvio del lifespan: niente di nostro per ora; uvicorn stampa `Application startup complete`.
- All'arrivo di una delivery `pull_request`: uvicorn logga la POST a 202; il task in background in caso di errore logga `agent run failed` con `repo`, `pr_number`, `installation_id`.
- Quando `ANTHROPIC_API_KEY` o le credenziali GitHub mancano: warn `agent runner not configured; skipping dispatch`.

## Scenari NON coperti (volutamente)

- **Token usage / costo per chiamata**: lo `state["tokens_used"]["triage"]` è seedato a `0`. Il tracking reale dei token (callback langchain) e il persist in Postgres arrivano in W2/W4. Per ora sappi solo che ogni delivery `pull_request` consuma un piccolo round con Haiku — costo medio < $0.001 per PR.
- **Retry del task background**: `BackgroundTasks` non ha retry policy. Se il post-comment fallisce, il commento è perso. In W3 con il rate limiter aggiungeremo retry+backoff.
- **Idempotency**: GitHub re-invia delivery dopo 5xx; `X-GitHub-Delivery` non viene ancora deduplicato. Significa che se il primo run fallisce a metà e GitHub re-invia, posteresti due "Hello". Tollerabile in W1; da affrontare prima del deploy.

## Riferimenti file

Codice consegnato in Task 5:

- `src/pr_review_agent/agent/state.py` — `AgentState` con `NotRequired[...]` sui campi che si popolano lungo il grafo.
- `src/pr_review_agent/agent/models.py` — `ChangeType`, `RiskLevel`, `ReviewDepth`, `TriageDecision`.
- `src/pr_review_agent/agent/nodes/triage.py` — `make_triage_node(chain)`.
- `src/pr_review_agent/agent/nodes/publisher.py` — `make_publisher_node(client)`.
- `src/pr_review_agent/agent/prompts/triage.md` — system prompt del triage.
- `src/pr_review_agent/agent/graph.py` — `build_graph(triage, publisher)`.
- `src/pr_review_agent/agent/runner.py` — `make_default_runner(...)` per produzione.
- `src/pr_review_agent/github/client.py` — `GitHubClient.post_pr_comment(...)`.
- `src/pr_review_agent/main.py` — lifespan compone http client + auth + client + runner in `app.state`.
- `src/pr_review_agent/webhook.py` — schedula `runner` come BackgroundTask, con `_run_agent_safely` che cattura eccezioni.
- `src/pr_review_agent/config.py` — `+anthropic_api_key`, validator esteso.

Test:

- `tests/unit/test_node_triage.py` — 3 test sul nodo triage.
- `tests/unit/test_node_publisher.py` — 3 test sul nodo publisher.
- `tests/unit/test_graph.py` — 1 test e2e sul grafo.
- `tests/unit/test_github_client.py` — 2 test sul client GitHub.
- `tests/unit/test_webhook_security.py` — `+test_webhook_schedules_agent_run`.

Bruno: invariata rispetto a Task 4 (12 request, 24 assert). Il dispatch in background è verificato dai pytest (`test_webhook_schedules_agent_run`) e dal test e2e §5.
