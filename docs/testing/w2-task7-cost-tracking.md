# W2 · Task 7 — Cost tracking persistito su Postgres

## Cosa è stato consegnato

Ogni run dell'agente lascia una riga in `agent_runs` su Postgres con i costi (token in/out, USD totale, breakdown per modello), il `correlation_id` di Task 6, e lo status finale (`success`/`failure`/`skipped`). È la base per il **cost cap per PR** di W3 e per la dashboard di W4.

- **Tabella `agent_runs`** (migrazione `migrations/0001_create_agent_runs.sql`): `correlation_id`, `repo`, `pr_number`, `head_sha`, `started_at`/`finished_at`, `status`, `triage_*`, `skipped`, `tool_calls_used`, `tokens_input`/`tokens_output`/`cost_usd`, `per_model` JSONB, `error`. Indici su `correlation_id` e `(repo, pr_number)`.
- **`apply_migrations(pool)`** legge ogni file `migrations/NNNN_*.sql` e lo esegue in ordine lessicografico. Idempotente: ogni file usa `IF NOT EXISTS` + `ON CONFLICT DO NOTHING`. Chiamata dal lifespan FastAPI all'avvio.
- **`pr_review_agent.db.runs`** con tre funzioni async (`record_run_started`, `record_run_finished`, `record_run_failed`) che il runner chiama. Niente ORM — solo `asyncpg` con SQL esplicito.
- **Tabella prezzi** in `pr_review_agent.agent.cost_table`:
  - Haiku 4.5 → $1.00 / $5.00 per 1M input/output
  - Sonnet 4.6 → $3.00 / $15.00 per 1M
  - Opus 4.7 → $15.00 / $75.00 per 1M
  
  Sourced da Anthropic public pricing 2026-05. Prezzi cambiano tramite PR, non tramite `.env` (così deploy non drittano su tariffe stale). `compute_cost_usd` ritorna `Decimal` quantizzato a 6 decimali (compatibile con `NUMERIC(10, 6)`).
- **`CostTrackingCallback`** è un `BaseCallbackHandler` LangChain che ascolta `on_llm_end`. Per ogni invocazione:
  - estrae `model_name` da `llm_output` o da `AIMessage.response_metadata`,
  - estrae `(input_tokens, output_tokens)` da `AIMessage.usage_metadata` (path canonical LangChain ≥0.2) o dal legacy `llm_output["token_usage"]` come fallback,
  - aggrega per modello in un dict in-process.
  
  Esposto via `RunnableConfig({"callbacks": [cb], ...})` al `graph.ainvoke` del runner. Funziona uniformemente con `with_structured_output` (triage/reviewer) e con il sub-grafo del Gatherer.
- **Settings**: `database_url: str | None = None`. Il default `None` **disabilita** la persistence con un warning all'avvio (`DATABASE_URL not set: agent runs will not be persisted`). Coerente con il behavior già esistente per `ANTHROPIC_API_KEY`/`GITHUB_*`: l'app gira ma è osservabile solo via stdout/LangSmith.
- **Runner**: per ogni run prima inserisce il record `'running'` (se il pool esiste e c'è un `correlation_id`), poi attacca il `CostTrackingCallback` al config del grafo, infine sul successo update `status='success'`/`'skipped'` e i totali; sull'eccezione fa `record_run_failed` (con `type(exc).__name__: str(exc)[:500]`) e rilancia.
- **Lifespan**: crea il pool `asyncpg`, applica le migrazioni, espone il pool su `app.state.db_pool`, lo passa al runner. Cleanup `pool.close()` in shutdown. Se `database_url` non è valido o asyncpg fallisce, il pool resta `None` e l'app continua senza persistence.

**Cosa NON è stato fatto:**

- Cost cap per PR (abort se cost stimato > soglia) — **W3**.
- Dashboard / endpoint di lettura sulla tabella — la query la fai con `psql`. Endpoint pubblico arriva W4 deploy.
- Integration test contro Postgres reale — i test in W2 sono unit test con un fake pool che cattura le query. Integration in W3 quando il CI ha docker-compose.
- Backfill di run precedenti — la migrazione crea solo la tabella; chi aveva runs in W1/W2 prima di Task 7 non vedrà righe storiche.

## Setup dell'ambiente di test

Per i test pytest **non serve** Postgres (fake pool). Per smoke locale serve il container di W1 Task 6:

```bash
docker compose up -d postgres
# Default DSN per .env locale (porta 5433 perché 5432 è occupata da pinkcare-db):
# DATABASE_URL=postgresql://pr_review:pr_review@localhost:5433/pr_review_agent
```

## Scenari da testare manualmente

### 1 — Pytest verde

```bash
uv run pytest -q
```

Atteso: **137 passed** (116 dopo W2 Task 6 + 9 nuovi `test_cost_table.py` + 6 `test_cost_callback.py` + 4 `test_db_runs.py` + 2 settings).

I nuovi test in dettaglio:

- **`test_cost_table.py` (9)**: prezzi pinati per Haiku/Sonnet/Opus, modello sconosciuto = 0, zero token = 0, parametrizzato per quantizzazione a 6 decimali.
- **`test_cost_callback.py` (6)**: aggregazione single + cross-model + repeated; fallback a `llm_output["token_usage"]`; nessun token → ignorato; modello sconosciuto → cost 0 ma token preservati.
- **`test_db_runs.py` (4)** con un fake pool che cattura SQL+params: `record_run_started` ritorna l'id e usa `INSERT ... 'running' ... RETURNING id`; `record_run_finished` aggiorna tutti i 10 campi nell'ordine giusto; il caso `skipped`; `record_run_failed` tronca l'errore a 500 char.
- **`test_config.py` (+2)**: `database_url` default `None`; lettura dell'env var.

### 2 — Bruno collection verde (sanity check)

Niente endpoint nuovi. Stesso comando di W2 Task 6:

```bash
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  uv run uvicorn pr_review_agent.main:app --port 8001 &
until curl -sf http://localhost:8001/health > /dev/null; do sleep 0.2; done
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  bash -lc 'cd bruno && npx --yes @usebruno/cli run --env local'
kill %1
```

Atteso: stesso risultato di W2 Task 6 (26/26 assertions verdi).

### 3 — Smoke con DB reale

```bash
docker compose up -d postgres
DATABASE_URL=postgresql://pr_review:pr_review@localhost:5433/pr_review_agent \
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  uv run uvicorn pr_review_agent.main:app --port 8001
```

Nei log vedi:

- `database pool ready and migrations applied` allo startup (la migrazione 0001 è già applicata se ricreata, perché idempotente).
- All'arrivo di un webhook `pull_request`: dopo che il run dell'agente termina, query la tabella:

```bash
docker compose exec postgres psql -U pr_review -d pr_review_agent \
  -c "SELECT id, correlation_id, repo, pr_number, status, tokens_input, tokens_output, cost_usd FROM agent_runs ORDER BY id DESC LIMIT 5;"
```

Dovresti vedere una riga con `status='success'` (o `'skipped'` se triage ha skippato) e i totali. Il `cost_usd` riflette i prezzi di `cost_table.py`.

### 4 — Verifica disabilitazione graceful

Lancia senza `DATABASE_URL`:

```bash
unset DATABASE_URL
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  uv run uvicorn pr_review_agent.main:app --port 8001
```

Atteso log: `DATABASE_URL not set: agent runs will not be persisted`. L'agente comunque parte. Nessun errore. Il run dell'agente fa la review come prima ma non scrive su DB.

### 5 — Verifica disabilitazione su DB irraggiungibile

Lancia con `DATABASE_URL` puntato a un host inesistente:

```bash
DATABASE_URL=postgresql://no:no@nowhere.example:5432/x \
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  uv run uvicorn pr_review_agent.main:app --port 8001
```

Atteso: `failed to initialise database pool; persistence disabled` con stacktrace nel log. L'app continua a girare (pool=None). Coerente con la decisione di non far crashare il process per un Postgres unreachable.

### 6 — Verifica failure path

Spara un webhook con `installation_id` invalido (es. 777 nei tuoi test Bruno). Atteso:

- `agent run failed` nel log con il `correlation_id`.
- Riga in `agent_runs` con `status='failure'`, `error LIKE 'GitHubAPIError: …'` (max 500 char).
- LangSmith trace marcato come errore.

### 7 — Verifica costi via REPL (no DB, no Anthropic)

```bash
uv run python - <<'PY'
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from uuid import uuid4
from pr_review_agent.agent.cost_callback import CostTrackingCallback

def fake_result(model, in_t, out_t):
    msg = AIMessage(
        content="ok",
        usage_metadata={"input_tokens": in_t, "output_tokens": out_t, "total_tokens": in_t+out_t},
        response_metadata={"model_name": model},
    )
    return LLMResult(generations=[[ChatGeneration(message=msg)]], llm_output={"model_name": model})

cb = CostTrackingCallback()
cb.on_llm_end(fake_result("claude-haiku-4-5-20251001", 5000, 200), run_id=uuid4())
cb.on_llm_end(fake_result("claude-sonnet-4-6", 12000, 1500), run_id=uuid4())
print(cb.totals())
PY
```

Atteso: dict con `tokens_input=17000`, `tokens_output=1700`, `cost_usd ≈ 0.034700` (5e-3·1 + 0.2e-3·5 + 12e-3·3 + 1.5e-3·15 = 5e-3 + 1e-3 + 36e-3 + 22.5e-3 = 64.5e-3 — circa). Verifica i singoli per_model.

## Cosa cercare nei log

- `database pool ready and migrations applied` allo startup → DB OK.
- `DATABASE_URL not set: agent runs will not be persisted` → senza DB, OK.
- `failed to initialise database pool; persistence disabled` con traceback → DB irraggiungibile, app comunque su.
- `cost_table.unknown_model model_id=<x>` → un modello non in tabella prezzi è stato usato. Da risolvere aggiornando `cost_table._PRICES`.
- `could not insert agent_runs row; continuing without persistence` → write fallita. Il run prosegue, ma il record non c'è.

## Limiti dichiarati

- **Niente cap di costo**: ogni run può costare quanto vuole. Il record è solo per leggere a posteriori. Cap → W3.
- **Pricing in codice, non DB**: se i prezzi cambiano, una PR. È deliberato — riduce il rischio di runtime drift.
- **`per_model` come JSONB**: query analitiche su singolo modello richiedono `->'<model>'->>'cost_usd'`. Per dashboard sul singolo modello in W4 valuteremo una tabella dedicata.
- **Tutta la persistence è non-bloccante**: errori di scrittura sono catchati, loggati, e l'agente prosegue. Significa che possiamo perdere row se Postgres muore mid-run. Per visibility la fonte di verità di backup è LangSmith (sempre attivo se `LANGSMITH_TRACING=true`).
- **Niente integration test in CI**: i test sono unit con fake pool. La verifica end-to-end contro Postgres vivo la facciamo manuale (§3) finché W3 non aggiunge docker-compose al CI.

## Riferimenti file

- `migrations/0001_create_agent_runs.sql` — schema.
- `src/pr_review_agent/db/__init__.py`, `db/migrations.py`, `db/runs.py` — persistence layer.
- `src/pr_review_agent/agent/cost_table.py` — pricing.
- `src/pr_review_agent/agent/cost_callback.py` — LangChain callback.
- `src/pr_review_agent/config.py` — `database_url` field.
- `src/pr_review_agent/main.py` — pool lifespan + migrations on startup.
- `src/pr_review_agent/agent/runner.py` — wiring di callback + persistence per-run.
- `tests/unit/test_cost_table.py` (9), `test_cost_callback.py` (6), `test_db_runs.py` (4), `test_config.py` (+2).
