# W2 · Task 6 — Correlation ID nei log strutturati

## Cosa è stato consegnato

Ogni delivery del webhook GitHub viene associata a un identificatore univoco — il **correlation ID** — che attraversa tutto il run dell'agente: log strutturati, response del webhook, metadati LangSmith. È il prerequisito per debugging in produzione (W4 deploy) e per il cost tracking di Task 7.

- **`pr_review_agent.observability.correlation`** — modulo nuovo con due funzioni:
  - `bind_correlation_id(*, request_id) -> str` — registra il correlation ID sul scope `structlog.contextvars`. Se `request_id` è `None`/vuoto genera un `uuid4().hex` (32 char hex). Ritorna l'ID effettivo.
  - `clear_correlation()` — rimuove il binding (sicuro anche se nulla è bound).
- **Sorgente del correlation ID**: header `X-GitHub-Delivery` di GitHub (UUID per delivery, retried con lo stesso valore). Fallback uuid quando l'header manca (test, replay manuali).
- **Webhook handler:** binding al primissimo step (prima della signature check) → ogni log emesso nel processing della request porta `correlation_id`. Il **background task** che esegue l'agent si re-binda esplicitamente all'ingresso (i contextvars normalmente propagano in `asyncio.create_task` ma siamo difensivi). Cleanup nel `finally` del wrapper background.
- **Response 202** ora include `delivery_id` — sia per `pull_request` accepted che per eventi ignorati (`ping`, ecc.). Ti permette di copia-incollare l'ID dal log GitHub al nostro stack di osservabilità.
- **Runner → LangSmith**: il runner legge `correlation_id` dal contextvars (binding già fatto da `_run_agent_safely`) e passa `RunnableConfig({"metadata": {"correlation_id": cid}, "tags": [f"correlation:{cid}"]})` a `graph.ainvoke`. LangSmith espone i tag come filtri top-level e i metadata in dettaglio del trace.
- **Bruno**: aggiornata la collection — `post-webhook-pull-request.bru` e `post-webhook-ping.bru` ora mandano `X-GitHub-Delivery` e asseriscono `res.body.delivery_id`.

**Cosa NON è stato fatto:**

- Persistenza dei correlation ID. Vivono in stdout / LangSmith. Tabella Postgres con join `correlation_id → cost` arriva W2 Task 7.
- Middleware FastAPI per il binding pre-handler. Lo facciamo dentro l'handler stesso. Quando aggiungiamo altri endpoint (W3/W4) potremo valutare un middleware per uniformare.
- `correlation_id` nei log dei test pytest stessi (lo verifichiamo solo via `structlog.contextvars.get_contextvars()`).

## Setup dell'ambiente di test

Nessuna dipendenza nuova. Tutto offline.

## Scenari da testare manualmente

### 1 — Pytest verde

```bash
uv run pytest -q
```

Atteso: **116 passed** (108 dopo W2 Task 5 + 5 nuovi `test_correlation.py` + 3 nuovi in `test_webhook_security.py`).

I nuovi test:

- **`test_correlation.py` (5)**: bind con request_id reale; bind con `None` → uuid hex; bind con stringa vuota → uuid hex; clear rimuove correttamente; clear è safe se nulla è bound.
- **`test_webhook_security.py` (+3)**:
  - `test_valid_signature_pull_request_returns_202` — esteso: assert su presenza `delivery_id` (uuid fallback quando l'header non è inviato).
  - `test_response_echoes_x_github_delivery_header` — quando il client invia `X-GitHub-Delivery: abc-123-from-github`, è quello il `delivery_id` nella response.
  - `test_ignored_event_response_includes_delivery_id` — anche su `ping` (ignored) torna `delivery_id`.
  - `test_background_task_binds_correlation_id_for_logs` — un fake runner che ispeziona `structlog.contextvars.get_contextvars()` vede `correlation_id` bound al delivery id.

### 2 — Bruno collection verde

```bash
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  uv run uvicorn pr_review_agent.main:app --port 8001 &
until curl -sf http://localhost:8001/health > /dev/null; do sleep 0.2; done
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  bash -lc 'cd bruno && npx --yes @usebruno/cli run --env local'
kill %1
```

Atteso: tutti i `assert` verdi, in particolare `res.body.delivery_id` su `post-webhook-pull-request.bru` (eq `00000000-0000-0000-0000-000000000001`) e `post-webhook-ping.bru` (eq `00000000-0000-0000-0000-000000000002`).

### 3 — Smoke via curl

```bash
GITHUB_WEBHOOK_SECRET=demo uv run uvicorn pr_review_agent.main:app --port 8001 &

BODY='{"action":"opened","number":1,"pull_request":{"number":1,"title":"t","head":{"ref":"x","sha":"a"},"base":{"ref":"main","sha":"b"}},"repository":{"full_name":"f/p"},"installation":{"id":99}}'
SIG="sha256=$(printf %s "$BODY" | openssl dgst -sha256 -hmac demo | awk '{print $2}')"

# Caso 1: passi tu il delivery id → l'echo è quello
curl -s -X POST http://localhost:8001/webhook/github \
  -H "X-GitHub-Event: pull_request" \
  -H "X-GitHub-Delivery: my-custom-id" \
  -H "X-Hub-Signature-256: $SIG" \
  -d "$BODY"

# Caso 2: niente header → uuid generato
curl -s -X POST http://localhost:8001/webhook/github \
  -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: $SIG" \
  -d "$BODY"

kill %1
```

Atteso:

- Caso 1: `{"status":"accepted","pr":1,"repo":"f/p","installation_id":99,"delivery_id":"my-custom-id"}`.
- Caso 2: `delivery_id` è una stringa hex di 32 caratteri (uuid4 senza tratti). Nei log strutturati la stessa riga di `agent run failed` (se il runner non c'è) o il warning `agent runner not configured` porta `correlation_id`.

### 4 — Verifica LangSmith metadata (manuale)

In ambiente con `LANGSMITH_TRACING=true`, dopo aver mandato un webhook, apri la run su LangSmith. Atteso:

- Il run principale ha **tag** `correlation:<id>` visibile nella sidebar.
- I **metadata** del run includono `correlation_id: <id>`.
- Filtri rapidi tramite la barra di ricerca: `tag:"correlation:<id>"` mostra tutti i sub-run associati.

### 5 — Verifica isolamento (no leak tra delivery)

Spara due webhook in fila con delivery ID diversi. Nei log structlog (in console dev, modalità `ConsoleRenderer`) ogni delivery deve mostrare il **proprio** `correlation_id` su tutte le sue righe; nessuna riga della seconda delivery deve portare l'ID della prima. Il `clear_correlation()` nel `finally` di `_run_agent_safely` è il pezzo che garantisce questo.

## Cosa cercare nei log

- Ogni log line di un run dell'agente ha `correlation_id=<id>` come campo strutturato. Esempio (modalità JSON):
  ```json
  {"event":"agent run failed","correlation_id":"abc-123","repo":"f/p",...}
  ```
- Il `correlation_id` è lo stesso di `delivery_id` nella response 202. Se l'header `X-GitHub-Delivery` non c'era, è un hex di 32 caratteri.

## Limiti dichiarati

- **Binding solo dentro `_run_agent_safely`**: se in futuro aggiungiamo altri entry point (es. CLI, un altro endpoint HTTP) ognuno deve fare il bind/clear esplicitamente. Risolveremo con un middleware ASGI quando avremo > 2 entry point.
- **Niente sampling/redaction**: il `correlation_id` finisce in chiaro nei log. È OK perché non è un secret — è un UUID. Ma se in futuro mappiamo `correlation_id → user identity` quel mapping non deve finire in stdout.
- **`delivery_id` nella response 202** è informativo, non parte di un contratto pubblico. Se in W3/W4 dovesse cambiare nome (es. `request_id`) lo notifichiamo nelle release notes.

## Riferimenti file

- `src/pr_review_agent/observability/correlation.py` — bind/clear.
- `src/pr_review_agent/webhook.py` — bind nell'handler + re-bind nel background task + echo nella response.
- `src/pr_review_agent/agent/runner.py` — `RunnableConfig` con metadata + tag.
- `tests/unit/test_correlation.py` — 5 test.
- `tests/unit/test_webhook_security.py` — 3 nuovi test, 1 esteso.
- `bruno/webhook/post-webhook-pull-request.bru` — assert `delivery_id`.
- `bruno/webhook/post-webhook-ping.bru` — assert `delivery_id`.
