# W1 · Task 2 — FastAPI scaffold + Pydantic Settings + Bruno collection

## Cosa è stato consegnato

Il perimetro HTTP minimo dell'agente: `GET /health` (uptime probe) e `POST /webhook/github` (stub che accetta qualsiasi body e risponde 202). Configurazione applicativa via `pydantic-settings` con quattro campi (`environment`, `log_level`, `cost_cap_per_pr_usd`, `max_tool_calls_per_node`) e logging strutturato `structlog` configurato all'avvio (JSON fuori da `development`, console renderer in dev). La Bruno collection in `bruno/` esercita gli endpoint via CLI o GUI con happy path e prima copertura di errori.

**Cosa NON è ancora stato fatto (rimandato a task successive):**
- Verifica firma `X-Hub-Signature-256` sul webhook (Task 3).
- Validazione del payload `pull_request` (Task 3).
- JWT GitHub App + installation token (Task 4).
- Background dispatch verso il grafo LangGraph (Task 4-5).

## Setup dell'ambiente di test

Da una shell pulita nella root del progetto:

```bash
uv sync                                                 # crea/aggiorna .venv
uv run uvicorn pr_review_agent.main:app --port 8001     # server in foreground
```

In una seconda shell userai `curl` o Bruno per esercitare gli endpoint.

**Note importanti:**
- La **porta 8001** è il default in dev perché sulla macchina WSL c'è un altro servizio sulla 8000. Se la tua 8000 è libera, cambia `bruno/environments/local.bru` e il flag `--port` di uvicorn.
- Nessuna env reale è richiesta per Task 2: non si chiamano API esterne. Lavorare senza `.env` è esplicitamente supportato (i `Settings` cadono sui default).

## Scenari da testare manualmente

### 1 — Happy: `GET /health` risponde 200 OK

```bash
curl -i http://localhost:8001/health
```

Atteso: status `200 OK`, body `{"status":"ok"}`. Se vedi un body diverso (per esempio con un campo `model_loaded`) probabilmente stai colpendo il servizio sulla 8000, non il nostro: ricontrolla la porta.

### 2 — Happy: `POST /webhook/github` accetta qualsiasi JSON

```bash
curl -i -X POST http://localhost:8001/webhook/github \
  -H 'Content-Type: application/json' \
  -H 'X-GitHub-Event: pull_request' \
  -d '{"action":"opened","number":1}'
```

Atteso: `202 Accepted`, body `{"status":"accepted"}`. La rapida risposta è voluta: GitHub si aspetta un 2xx in pochi secondi, e il lavoro vero (review della PR) sarà dispatchato in background dalle task successive.

### 3 — Errore: metodo non permesso su `/health`

```bash
curl -i -X POST http://localhost:8001/health
```

Atteso: `405 Method Not Allowed` con header `allow: GET`. Significato: FastAPI ha riconosciuto la rotta ma rifiuta il verbo, esattamente quello che vogliamo.

### 4 — Errore: metodo non permesso su `/webhook/github`

```bash
curl -i http://localhost:8001/webhook/github
```

Atteso: `405 Method Not Allowed` con header `allow: POST`. Stesso significato del punto 3.

### 5 — Errore: rotta inesistente

```bash
curl -i http://localhost:8001/healthz
curl -i -X POST http://localhost:8001/webhook/githubz -d '{}'
```

Atteso: in entrambi i casi `404 Not Found` con body `{"detail":"Not Found"}` (default FastAPI). Ti serve a confermare che non abbiamo mounted route nascoste o duplicate.

### 6 — Comportamento volutamente permissivo: body vuoto sul webhook (oggi 202, domani 400)

```bash
curl -i -X POST http://localhost:8001/webhook/github \
  -H 'Content-Type: application/json' -d '{}'
```

Atteso oggi: `202 Accepted`. Lo stub *non* valida ancora nulla. Quando arriveremo a Task 3 questo stesso caso diventerà `400 Bad Request` (firma mancante) o `422` (payload non riconosciuto). Lo registriamo già nella collection Bruno come `bruno/webhook/exceptions/202-empty-body-pre-task3.bru` così che, quando cambierà, se ne discuta esplicitamente.

### 7 — Logging: in dev vedi console renderer

Nello shell del server, durante le richieste sopra, dovresti vedere log human-readable (con colori se il terminale li supporta) — è il `ConsoleRenderer` di `structlog`. Niente JSON.

### 8 — Logging: in non-dev passi a JSON

Riavvia il server con `ENVIRONMENT=production`:

```bash
ENVIRONMENT=production uv run uvicorn pr_review_agent.main:app --port 8001
```

Rifai una richiesta `GET /health` e nello shell del server: i log applicativi del modulo dovrebbero arrivare come **una riga JSON per evento**. In Task 2 i log applicativi sono pochi (solo l'init del lifespan), quindi non aspettarti un fiume; quello che importa è che il *renderer* sia cambiato. La verifica vera arriva quando aggiungiamo log per richiesta in W2.

### 9 — `LOG_LEVEL` è rispettato

```bash
LOG_LEVEL=DEBUG uv run uvicorn pr_review_agent.main:app --port 8001
```

Atteso: il root logger Python passa a `DEBUG`. Test `tests/unit/test_logging.py` lo verifica programmaticamente; lato manuale ti basta sapere che il flag è già sotto controllo.

### 10 — Bruno collection: happy + exception in un comando

```bash
# In una seconda shell, server già su 8001
cd bruno
npx --yes @usebruno/cli run --env local
```

Atteso: 9 request, 14 assert, tutti verdi. La collection include 4 happy (health, webhook stub, e2e step 1, e2e step 2) e 5 exception (`health/exceptions/{405-post,404-typo}.bru`, `webhook/exceptions/{405-get,404-typo,202-empty-body-pre-task3}.bru`).

**Lavorare nella GUI Bruno.** Prima di aprire qualunque `.bru` nella GUI desktop, seleziona l'environment **"local"** dal dropdown in alto a destra. Senza env attivo la GUI non risolve `{{baseUrl}}` e inietta `vars:pre-request { baseUrl: http://localhost:8000 }` nei file aperti — un override hardcoded che rompe il run da CLI. Con l'env attivo la GUI rispetta le variabili e non tocca i file. Convenzione fissata in `CLAUDE.md`.

## Cosa cercare nei log

In Task 2 i log applicativi sono ancora pochi:

- All'avvio del lifespan: niente di esplicito nostro, ma `uvicorn` stampa `Started server process` e `Application startup complete` — se non li vedi, il server non è partito.
- Durante le richieste: `uvicorn` logga ogni accesso (`200 OK`, `405 Method Not Allowed`, ecc.). Il logger applicativo nostro entrerà in scena con il middleware di correlation-ID in W2.

## Scenari di errore NON coperti in Task 2 (volutamente)

Dichiarazione esplicita per evitare interrogativi durante la review:

- **401 / 403** — non producibili oggi. L'autenticazione GitHub App arriva in Task 4. La verifica firma webhook in Task 3.
- **422 validation** — non producibile: non c'è ancora un Pydantic model che valida il body del webhook. Arriva in Task 3.
- **5xx** — nessun downstream esterno è chiamato in questo perimetro, quindi nessun timeout / errore upstream. Arriverà quando il webhook attiverà il dispatch del grafo LangGraph (Task 4-5) e quando si parlerà con Anthropic.

## Riferimenti file

Codice consegnato in Task 2:

- `src/pr_review_agent/main.py` — composizione `app: FastAPI`, lifespan, montaggio router.
- `src/pr_review_agent/health.py` — router `GET /health`.
- `src/pr_review_agent/webhook.py` — router `POST /webhook/github` stub.
- `src/pr_review_agent/config.py` — `Settings` + `get_settings()` lru-cached.
- `src/pr_review_agent/observability/logging.py` — `configure_logging()`.

Test:

- `tests/conftest.py` — fixture `client: TestClient`.
- `tests/unit/test_health.py`, `tests/unit/test_webhook_endpoint.py`, `tests/unit/test_config.py`, `tests/unit/test_logging.py`.

Bruno:

- `bruno/health/get-health.bru`, `bruno/webhook/post-webhook-stub.bru`, `bruno/e2e/0[12]-*.bru`.
- `bruno/health/exceptions/{405-post,404-typo}.bru`.
- `bruno/webhook/exceptions/{405-get,404-typo,202-empty-body-pre-task3}.bru`.
- `bruno/environments/local.bru`.
