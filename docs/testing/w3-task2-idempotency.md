# W3 · Task 2 — Idempotency su `X-GitHub-Delivery`

## Cosa è stato consegnato

Una protezione contro le **redelivery automatiche di GitHub**: quando GitHub non riceve risposta entro ~10s o riceve un 5xx, ripete il webhook con lo stesso `X-GitHub-Delivery`. Senza guardrail il nostro agente eseguirebbe due review sulla stessa PR, raddoppiando il costo e creando rumore con commenti doppi.

- **Lookup `find_run_by_correlation_id(pool, correlation_id) -> int | None`** in `db/runs.py`. Una sola query: `SELECT id FROM agent_runs WHERE correlation_id = $1 LIMIT 1`. L'indice `agent_runs_correlation_id_idx` di Task 7 lo rende O(log n).
- **Idempotency check nel webhook handler** *prima* del dispatch. Triggerato solo quando:
  1. l'header `X-GitHub-Delivery` è presente (non il path uuid-fallback dei test/replay), **e**
  2. `app.state.db_pool` non è `None`.
  
  Se il check trova una riga → response `{"status": "duplicate", "delivery_id": …, "first_seen_run_id": …}`, niente dispatch. Se non la trova → flusso normale → `status='accepted'`.
- **Duplicate-policy: tutto, indipendentemente dallo status**. Se la riga è `running`, `success`, `skipped`, `failure` o `aborted_cost`, è una duplicate. GitHub retry implica timeout o 5xx → in entrambi i casi non vogliamo un secondo run. Se l'utente vuole rilanciare, fa un nuovo commit (`synchronize` con delivery_id diverso).
- **Fallback graceful**: se la query DB raise → log warning `idempotency_check_failed; dispatching anyway` e dispatch comunque. Availability vale più della idempotency assoluta.
- **Nessuna nuova tabella** — riusa `agent_runs`.
- **Bruno**: `bruno/webhook/post-webhook-pull-request-duplicate.bru` (seq: 3) riusa lo stesso `X-GitHub-Delivery` del happy-path (seq: 1) e asserisce `status: duplicate`. Richiede DB attivo (documentato nel `docs` block del file).

**Cosa NON è stato fatto:**

- Endpoint admin per "force re-run" — fuori scope. Re-run = nuovo commit.
- TTL sul check (es. "duplicate solo se entro 24h"). Le delivery di GitHub usano UUID stabili: una vecchia delivery ID non collide mai con una nuova.
- Lock in DB con `SELECT ... FOR UPDATE` per race tra duplicate ravvicinate. La race c'è teoricamente (due delivery in 50ms, entrambe vedono "no row" → entrambe dispatch) ma il `RETURNING id` di `record_run_started` non protegge da questo: dovremmo aggiungere un `UNIQUE(correlation_id)` constraint per chiudere il caso. Lo facciamo se in produzione vediamo davvero double-runs nei log.

## Setup dell'ambiente di test

Test pytest sono offline (fake pool). Il test Bruno duplicate richiede DB attivo.

## Scenari da testare manualmente

### 1 — Pytest verde

```bash
uv run pytest -q
```

Atteso: **151 passed** (144 dopo W3-Task1 + 5 nuovi `test_webhook_security.py` + 2 nuovi `test_db_runs.py`).

I nuovi test:

- **`test_db_runs.py` (+2)**:
  - `test_find_run_by_correlation_id_returns_id_on_hit` — verifica SQL (`SELECT id FROM agent_runs WHERE correlation_id = $1 LIMIT 1`) e ritorno dell'id.
  - `test_find_run_by_correlation_id_returns_none_on_miss` — `fetchval` ritorna `None` quando il fake pool non ha il record.
- **`test_webhook_security.py` (+5)**:
  - `test_duplicate_delivery_skips_dispatch` — `find_run_by_correlation_id` mockata per ritornare `7` → response `{"status":"duplicate","first_seen_run_id":7}` e il fake runner **non** viene mai chiamato.
  - `test_first_seen_delivery_is_dispatched` — `find` ritorna `None` → flusso normale, runner chiamato.
  - `test_idempotency_check_failure_falls_back_to_dispatch` — `find` raise → response `accepted`, runner chiamato comunque.
  - `test_idempotency_check_skipped_when_no_db_pool` — `app.state.db_pool = None` → check NON eseguito (asserito tramite contatore di chiamate alla `find` mockata).
  - `test_idempotency_check_skipped_when_no_upstream_delivery_header` — niente header → uuid fallback path → check NON eseguito.

### 2 — Bruno collection verde (con DB up)

```bash
docker compose up -d postgres
DATABASE_URL=postgresql://postgres:postgres@localhost:5433/pr_review_agent \
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  uv run uvicorn pr_review_agent.main:app --port 8001 &
until curl -sf http://localhost:8001/health > /dev/null; do sleep 0.2; done
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  bash -lc 'cd bruno && npx --yes @usebruno/cli run --env local'
kill %1
```

Atteso: 28/28 assertions verdi (le 26 esistenti + 2 nuove sul duplicate; il numero esatto dipende dalle altre exception requests che già c'erano).

Sequenza chiave:
- seq 1 (`post-webhook-pull-request.bru`): primo invio con delivery `00000000-…0001` → `status: accepted`.
- seq 3 (`post-webhook-pull-request-duplicate.bru`): stesso delivery → `status: duplicate`.

**Nota:** la collection può fallire al primo run pulito perché c'è già il record. Per una run "fresh" cancella la tabella prima:

```bash
docker compose exec postgres psql -U postgres -d pr_review_agent -c "TRUNCATE agent_runs;"
```

### 3 — Smoke E2E con curl + DB

Server up come §2. In altra shell:

```bash
SECRET=test-bruno-secret
BODY='{"action":"opened","number":99,"pull_request":{"number":99,"title":"smoke idempotency","body":"","head":{"ref":"feat/x","sha":"deadbeef"},"base":{"ref":"main","sha":"cafebabe"}},"repository":{"full_name":"francesco/playground"},"installation":{"id":777}}'
SIG="sha256=$(printf %s "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"

# Prima delivery: accepted
curl -s -X POST http://localhost:8001/webhook/github \
  -H "X-GitHub-Event: pull_request" \
  -H "X-GitHub-Delivery: smoke-idem-001" \
  -H "X-Hub-Signature-256: $SIG" \
  -d "$BODY"
echo

# Seconda delivery con stesso ID (simula retry GitHub): duplicate
curl -s -X POST http://localhost:8001/webhook/github \
  -H "X-GitHub-Event: pull_request" \
  -H "X-GitHub-Delivery: smoke-idem-001" \
  -H "X-Hub-Signature-256: $SIG" \
  -d "$BODY"
echo
```

Atteso:

```
{"status":"accepted","pr":99,"repo":"francesco/playground","installation_id":777,"delivery_id":"smoke-idem-001"}
{"status":"duplicate","pr":99,"repo":"francesco/playground","installation_id":777,"delivery_id":"smoke-idem-001","first_seen_run_id":1}
```

Nei log della seconda chiamata: `duplicate delivery; skipping dispatch delivery_id=smoke-idem-001 first_seen_run_id=1`.

In DB:

```bash
docker compose exec postgres psql -U postgres -d pr_review_agent -c \
"SELECT id, correlation_id, status FROM agent_runs WHERE correlation_id='smoke-idem-001';"
```

Atteso: **una sola riga**.

### 4 — DB irraggiungibile → graceful

Spegni Postgres mid-test:

```bash
docker compose stop postgres
```

Poi rimanda lo stesso curl di §3. Atteso: response `accepted` e nei log `idempotency_check_failed; dispatching anyway`. Il run dell'agente parte (fallirà su token GitHub fittizio, ma il punto è che il check non blocca). Ripristina:

```bash
docker compose start postgres
```

### 5 — Senza DB

```bash
unset DATABASE_URL
GITHUB_WEBHOOK_SECRET=test-secret uv run uvicorn pr_review_agent.main:app --port 8001
```

Atteso: warning `DATABASE_URL not set: agent runs will not be persisted`. Mandare due delivery con stesso ID → entrambe `accepted`, niente check (`db_pool=None`).

## Cosa cercare nei log

- `duplicate delivery; skipping dispatch delivery_id=… first_seen_run_id=…` (info) → idempotency ha bloccato un re-delivery. Tutto bene.
- `idempotency_check_failed; dispatching anyway` (warning) → DB irraggiungibile durante il check. Possibile duplicato in DB se la delivery viene mandata di nuovo dopo che il DB torna su.
- Niente di tutto questo + `agent_runs` con due righe stesso `correlation_id` → bug. Apri issue.

## Limiti dichiarati

- **Race tra delivery ravvicinate (~ms)**: due copie possono arrivare prima che la prima abbia inserito la riga. Entrambe vedono "no row" e dispatchano. Mitigation futura: `UNIQUE(correlation_id)` constraint + try/except sul `record_run_started`. Per ora il rischio è teorico (GitHub retry standard è ~30s, non ms).
- **`UNIQUE` constraint NON aggiunto in questa task**: implicherebbe migrazione e gestione errori di insert duplicate. Lo aggiungeremo se vediamo il bug in produzione.
- **`failure` → duplicate (no auto-retry)**: se un primo run fallisce, GitHub retry NON triggera un secondo tentativo. Il developer deve fare un nuovo commit (delivery_id diverso). Voluto: evita loop di retry su errori permanenti.
- **Bruno duplicate test richiede DB attivo**: documentato nel file. Se lo lanci senza DB il test fallisce, ma è un fail "atteso" (non una regressione).

## Riferimenti file

- `src/pr_review_agent/db/runs.py` — `find_run_by_correlation_id`.
- `src/pr_review_agent/db/__init__.py` — export aggiornato.
- `src/pr_review_agent/webhook.py` — idempotency check pre-dispatch.
- `tests/unit/test_db_runs.py` — 2 nuovi test.
- `tests/unit/test_webhook_security.py` — 5 nuovi test (4 path + class helper).
- `bruno/webhook/post-webhook-pull-request-duplicate.bru` — duplicate path.
