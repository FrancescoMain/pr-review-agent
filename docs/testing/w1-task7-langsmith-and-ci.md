# W1 · Task 7 — LangSmith tracing + GitHub Actions CI

## Cosa è stato consegnato

**LangSmith.** Ogni invocazione del grafo (e ogni call a Claude Haiku al suo interno) viene tracciata su https://smith.langchain.com/. Configurazione via env vars (`LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY=lsv2_pt_...`, `LANGSMITH_PROJECT=pr-review-agent`). Il lifespan di FastAPI legge le tre dalle `Settings` e le ri-esporta su `os.environ` (perché LangChain le legge da lì direttamente, non dall'oggetto Settings). Niente codice di tracing custom: l'auto-instrumentation di `langchain-core` fa il resto.

**GitHub Actions CI.** Workflow `ci.yml` che gira su `push` su `main` e su ogni `pull_request`. Esegue: install `uv`, `uv sync --frozen`, `ruff check`, `ruff format --check`, `pyright` strict, `pytest -v`. Cache delle dipendenze attivata su `uv.lock`. Concurrency policy: cancella i run vecchi sullo stesso branch quando ne arriva uno nuovo, per non sprecare minuti GitHub Actions.

**Cosa NON è stato fatto:**
- Trace upload da CI: pytest in CI non chiama Anthropic (i mock impediscono leak), quindi LangSmith dalla CI non riceve nulla — voluto, evita rumore sul free tier.
- Bruno run in CI: richiederebbe Postgres+Qdrant in service container e i `secrets` GitHub Actions per `GITHUB_WEBHOOK_SECRET`. Lo aggiungiamo in W4 quando la CI farà più cose (deploy, smoke).
- CD / deploy: W4.

## Setup dell'ambiente di test

Aggiungi al tuo `.env`:

```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_pt_...      # la key annotata in SETUP §2.3
LANGSMITH_PROJECT=pr-review-agent
```

Se non hai ancora fatto SETUP §2.3 (LangSmith free tier), va su `https://smith.langchain.com/`, sign-up con GitHub, crea un progetto chiamato `pr-review-agent`, **Settings → API Keys → Create API Key**, copia la `lsv2_pt_...`.

## Scenari da testare manualmente

### 1 — Pytest verde

```bash
uv run pytest -v
```

Atteso: **39 passed**. I 2 nuovi sono in `tests/unit/test_config.py`: `test_langsmith_defaults` (no env → tracing off, project default) e l'estensione di `test_settings_reads_env_vars` per i tre campi LangSmith.

### 2 — Bruno verde

```bash
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  uv run uvicorn pr_review_agent.main:app --port 8001 &
until curl -sf http://localhost:8001/health > /dev/null; do sleep 0.2; done
(cd bruno && GITHUB_WEBHOOK_SECRET=test-bruno-secret npx --yes @usebruno/cli run --env local)
kill %1
```

Atteso: **13 request, 26/26 assert** (invariato).

### 3 — Trace di una delivery vera su LangSmith

Stesso flusso di Task 5 §5 ma con `LANGSMITH_TRACING=true` nel `.env`:

1. `docker compose up -d` (se non già su).
2. `uv run uvicorn pr_review_agent.main:app --port 8001`. Nei log all'avvio: `LangSmith tracing enabled project=pr-review-agent`.
3. In altra shell: `ngrok http --url=witty-crow-heroic.ngrok-free.app 8001`.
4. Verifica che il Webhook URL della tua App punti a `https://witty-crow-heroic.ngrok-free.app/webhook/github`.
5. Apri una nuova PR sul `pr-review-agent-playground` (anche un altro branch con un piccolo cambio al README).
6. Apri https://smith.langchain.com/ → seleziona il progetto `pr-review-agent` → vedrai un nuovo run con i due step (`triage`, `publisher`) e, dentro `triage`, l'invocazione di `ChatAnthropic` con prompt + risposta strutturata + token usage + latency.

**Cosa cercare:** il run di alto livello "graph" deve mostrare ~2 spans (uno per nodo); il triage span deve avere un sotto-span `ChatAnthropic` con il prompt visibile e la risposta strutturata `TriageDecision`. Latency tipica: 1-3 s totali (la chiamata a Haiku domina).

### 4 — CI verde

Dopo aver pushato il branch su GitHub:

1. `https://github.com/FrancescoMain/pr-review-agent/actions` → vedi il workflow `CI` partire automaticamente sul push.
2. Click sul run → vedrai i 5 step (lint, format, pyright, pytest) tutti verdi entro ~2 minuti.
3. Quando aprirai un PR su `main`, lo stesso workflow gira automaticamente sul PR e ne vedi lo status nella pagina della PR.

Se il run è rosso, click sullo step rosso per vedere il log preciso (di solito è una divergenza tra il tuo locale e CI, tipo file aggiornato senza re-format).

## Cosa cercare nei log

- **Avvio uvicorn con LangSmith abilitato:** una linea structlog `LangSmith tracing enabled project=...`.
- **Avvio con LangSmith chiesto ma key vuota:** una linea `warning LangSmith tracing requested but LANGSMITH_API_KEY is empty`. Tipico se hai messo `LANGSMITH_TRACING=true` senza popolare la key.
- **Senza LangSmith** (default `LANGSMITH_TRACING=false`): nessuna linea relativa, nessun trace.

## Limiti dichiarati

- **Free tier LangSmith:** 5000 trace/mese. Per le 4 settimane di sviluppo è abbondante.
- **CI senza Bruno né Postgres:** GitHub Actions free tier ha 2000 minuti/mese privati; un run di CI passa in ~2 minuti. Se in W4 vorremo aggiungere lo smoke Bruno in CI, useremo `services:` per Postgres+Qdrant e GitHub `secrets` per il webhook secret.
- **`os.environ.setdefault`** invece di `os.environ[...] = ...`: se nello shell hai già esportato `LANGSMITH_*` quei valori vincono su `.env`. È voluto: comodo per disattivare il tracing al volo (`LANGSMITH_TRACING=false uv run uvicorn ...`).

## Riferimenti file

- `src/pr_review_agent/config.py` — `+langsmith_tracing`, `+langsmith_api_key`, `+langsmith_project`.
- `src/pr_review_agent/main.py` — `+_enable_langsmith_tracing(settings)` chiamato dal lifespan.
- `tests/unit/test_config.py` — `+test_langsmith_defaults`, e `test_settings_reads_env_vars` ora copre anche i tre nuovi campi.
- `.github/workflows/ci.yml` — workflow CI completo.
