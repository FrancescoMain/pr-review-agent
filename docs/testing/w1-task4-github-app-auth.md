# W1 · Task 4 — GitHub App auth (JWT + installation token con caching)

## Cosa è stato consegnato

Il blocco di **autenticazione** per la GitHub App. Dato un `installation_id`, l'agente ora sa generare un JWT App-level firmato RS256 con la `.pem`, scambiarlo via `POST /app/installations/{id}/access_tokens` per un installation token (TTL ~1h), e tenerlo in una **cache in-memory** con safety-margin di 5 minuti prima della scadenza. Le chiamate concorrenti sullo stesso `installation_id` condividono un singolo exchange grazie a un `asyncio.Lock` per-id (no thundering herd).

Il payload `pull_request` ora richiede il campo `installation.id`; il webhook handler lo estrae e lo include nella response (`{"installation_id": N, ...}`). **Niente uso effettivo del token in questa task** — chiamare l'API GitHub per commentare la PR è Task 5, dove il token viene consumato dal grafo LangGraph.

**Cosa NON è ancora stato fatto:**
- Dispatch in background del webhook verso il grafo (Task 5).
- Chiamate API GitHub per commentare PR (Task 5).
- Cache distribuita Postgres/Redis — quella arriverà in W4 se servirà più di un worker.

## Setup dell'ambiente di test

Per i pytest e per la Bruno collection è sufficiente quanto già hai (`uv sync`, `GITHUB_WEBHOOK_SECRET=...`). Per il **test reale contro la GitHub API** servono:

```bash
# In .env
GITHUB_APP_ID=<il numero che hai annotato in SETUP.md §3.2>
GITHUB_APP_PRIVATE_KEY_PATH=/home/cesar/.secrets/pr-review-agent.pem
GITHUB_WEBHOOK_SECRET=<il vero webhook secret della tua App>
```

La `.pem` deve esistere ed essere leggibile dall'utente che lancia uvicorn / lo script.

## Scenari da testare manualmente

### 1 — Pytest verde

```bash
uv run pytest -v
```

Atteso: **28 passed**. Le 8 nuove sono in `tests/unit/test_github_auth.py` (JWT, exchange, cache, refresh, errori 401/5xx, concurrent share). 1 nuova in `tests/unit/test_config.py` (validator che richiede credenziali fuori dev).

### 2 — Bruno collection con il payload aggiornato

```bash
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  uv run uvicorn pr_review_agent.main:app --port 8001
# In altra shell
export GITHUB_WEBHOOK_SECRET=test-bruno-secret
cd bruno && npx --yes @usebruno/cli run --env local
```

Atteso: **12 request, 24/24 assert** (era 22, +2 perché ora controlliamo `installation_id` nelle response happy). Il body delle request webhook firmate include `"installation":{"id":12345}` (e `"id":777` nell'e2e). Il `422-malformed-pull-request.bru` resta col body minimal e continua a triggerare 422 (mancano comunque diversi campi obbligatori, ora incluso `installation`).

### 3 — Smoke con la GitHub App reale

Solo manuale, contro `https://api.github.com`. Conferma che `GITHUB_APP_ID` + `.pem` + installation funzionano end-to-end.

```bash
uv run python scripts/get_installation_token.py
```

Atteso (output di esempio):

```
installation_id=51234567  account=cesarancesarano  expires_at=2026-05-05T16:35:12+00:00  token=ghs_abcd...wxyz
```

Una riga per ogni installation della tua App. Se la tua App è installata solo sul `pr-review-agent-playground` vedrai una sola riga. Per stampare il token completo (utile solo se devi fare un curl manuale):

```bash
uv run python scripts/get_installation_token.py --full
```

### 4 — Errore: `.pem` mancante o path sbagliato

```bash
GITHUB_APP_PRIVATE_KEY_PATH=/tmp/nonexistent.pem \
GITHUB_APP_ID=$GITHUB_APP_ID \
  uv run python scripts/get_installation_token.py
```

Atteso: `GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH must be set in .env and the .pem file must exist.` su stderr, exit code 2.

### 5 — Errore: `GITHUB_APP_ID` errato

Cambia `GITHUB_APP_ID` a un numero che non corrisponde a nessuna App e rilancia lo script. Atteso: lo script solleva `httpx.HTTPStatusError` con HTTP 401 da GitHub (perché GitHub rifiuta il JWT firmato con un `iss` sbagliato). Se preferisci un errore più amichevole posso wrappare `response.raise_for_status()` con il nostro `GitHubAuthError` — dimmelo.

### 6 — End-to-end con ngrok + GitHub App reale

Stesso flusso di Task 3 §9 ma con un controllo extra: ora la response del webhook contiene `installation_id`. Apri Recent Deliveries della tua App → ultima delivery `pull_request` → la response body deve includere `"installation_id": <numero>`. Quel numero è uguale a quello che vedi quando esegui `scripts/get_installation_token.py`.

## Cosa cercare nei log

Niente di nuovo rispetto a Task 3: il webhook handler non emette ancora log strutturati per delivery (li aggiungeremo in W2 col correlation-ID). `scripts/get_installation_token.py` stampa direttamente su stdout.

## Scenari NON coperti (volutamente)

- **Token scaduto a runtime** — il refresh-on-near-expiry è coperto dal pytest `test_get_installation_token_refreshes_when_near_expiry`. Non lo testiamo manualmente perché aspettare 55 minuti di TTL è scomodo; ti fidi del test.
- **GitHub API fuori uso (5xx)** — coperto in pytest con `respx`. Non riproduco una vera failure di GitHub.
- **Retry con backoff** — non implementato per design in Task 4. Quando in W3 aggiungeremo il rate-limiter, decideremo lì la policy di retry.

## Riferimenti file

Codice consegnato in Task 4:

- `src/pr_review_agent/github/auth.py` — `GitHubAppAuth`, `InstallationToken`.
- `src/pr_review_agent/github/exceptions.py` — `+GitHubAPIError`, `+GitHubAuthError`.
- `src/pr_review_agent/github/models.py` — `+Installation`, `PullRequestEvent.installation`.
- `src/pr_review_agent/config.py` — `+github_app_id`, `+github_app_private_key_path` con validator non-dev.
- `src/pr_review_agent/webhook.py` — response include `installation_id`.
- `scripts/get_installation_token.py` — CLI smoke contro l'API reale.

Test:

- `tests/unit/test_github_auth.py` — 8 test (JWT, exchange, cache, refresh, errors, concurrency).
- `tests/unit/test_config.py` — `+test_settings_validator_rejects_missing_credentials_in_production`.
- `tests/unit/test_webhook_security.py` — payload + assert aggiornati.

Bruno:

- `bruno/webhook/post-webhook-pull-request.bru`, `bruno/e2e/02-webhook-pull-request.bru` — body con `installation`, assert `installation_id`.
