# W3 · Task 3 — Rate limiting GitHub API

## Cosa è stato consegnato

Un guardrail reattivo sui rate limit di GitHub. Il `GitHubClient` legge gli header `X-RateLimit-*` da ogni response e, prima di ogni chiamata successiva, controlla se siamo sotto una soglia di sicurezza. Se lo siamo, dorme fino al reset (se vicino) o solleva `GitHubRateLimitError` (se troppo lontano). I 403/429 con `Retry-After` (rate limit secondario) seguono lo stesso path. Il runner cattura l'eccezione e abort il run come per il cost cap, postando un commento "rate limit hit, retry in Ns" e scrivendo `status='aborted_rate_limit'` su Postgres.

- **`Settings.github_rate_limit_floor: int = 100`** — quante richieste lasciare sempre in pancia. Sopra questo valore la chiamata procede normalmente.
- **`Settings.github_rate_limit_max_wait_seconds: int = 60`** — quanto siamo disposti ad aspettare il reset. Sopra → fail fast.
- **`GitHubRateLimitError(GitHubAPIError)`** in `github/exceptions.py`, con `retry_after_seconds: int | None`.
- **`GitHubClient`** ora tiene state in memoria (`_rate_remaining`, `_rate_reset_epoch`):
  - `_record_rate_limit(response)` — chiamato dopo **ogni** response, fa snapshot degli header.
  - `_maybe_wait_for_reset()` — chiamato all'inizio di **ogni** verb. Se sotto floor: `await asyncio.sleep(wait + 1)` se `wait <= max_wait`, altrimenti raise.
  - `_check_for_rate_limit_error(response)` — chiamato dopo ogni response: se status è 403/429 con `Retry-After`, raise `GitHubRateLimitError`. 403 senza `Retry-After` (permission denied) NON viene mappato a rate limit — resta `GitHubAPIError`.
- **Runner** cattura `GitHubRateLimitError` come secondo abort path graceful (dopo `CostCapExceeded`). `_abort_for_rate_limit` posta `⏳ Review aborted: GitHub API rate limit hit. Retry in ~Ns. Re-run by pushing a new commit once the limit resets.` e fa `record_run_finished(status='aborted_rate_limit', ...)` con i totali parziali.
- **`agent_runs.status`** accetta il nuovo valore `'aborted_rate_limit'`. Niente migrazione: il campo è `TEXT`.
- **Stato per-process**: ogni worker uvicorn ha il suo client. Se due worker condividono lo stesso installation token, eventualmente tutti vedranno il floor e si bloccheranno reattivamente. Niente coordinamento esplicito.

**Cosa NON è stato fatto:**

- Token bucket pre-emptive client-side (overengineering per il volume attuale).
- Retry automatico decoratore-style (decisione esplicita: il developer rilancia con un nuovo commit dopo il cooldown).
- Coordinamento tra worker via DB / Redis. Lo aggiungiamo se in produzione vediamo che i worker pestano.
- Cap separato per `Search/Code` API (30 req/min). Non usiamo Search API oggi; quando il tool `recall_conventions` di W3-Task5 la userà, la consideriamo.

## Setup dell'ambiente di test

Nessuna dipendenza nuova. Test pytest sono offline (respx mocka gli header).

## Scenari da testare manualmente

### 1 — Pytest verde

```bash
uv run pytest -q
```

Atteso: **163 passed** (151 dopo W3-Task2 + 6 nuovi `test_github_client.py` + 4 nuovi `test_runner_rate_limit.py` + 2 nuovi `test_config.py`).

I nuovi test:

- **`test_github_client.py` (+6)**:
  - `test_client_records_rate_limit_headers_after_call` — dopo una `post_pr_comment`, lo state interno del client riflette gli header.
  - `test_client_sleeps_when_under_floor_and_reset_is_close` — seed sotto floor + reset 5s → `asyncio.sleep` chiamato (mockato), poi la chiamata procede.
  - `test_client_raises_when_under_floor_and_reset_is_far_away` — reset 10 min → raise `GitHubRateLimitError(retry_after_seconds=600)`.
  - `test_client_raises_on_403_with_retry_after` — 403 + `Retry-After: 120` → raise con quel valore.
  - `test_client_raises_on_429_with_retry_after` — stesso path su 429.
  - `test_client_403_without_retry_after_is_plain_api_error` — 403 senza header (es. permesso negato) → `GitHubAPIError` semplice, **non** `GitHubRateLimitError`.
- **`test_runner_rate_limit.py` (4)**:
  - `test_abort_posts_comment_with_retry_after_and_records_aborted_rate_limit` — happy path: comment con `120s`, DB `status='aborted_rate_limit'`, totali parziali presenti.
  - `test_abort_message_omits_retry_blurb_when_no_retry_after` — `retry_after_seconds=None` → comment senza "Retry in Ns".
  - `test_abort_does_not_raise_if_comment_post_fails` — la post del comment trigge a sua volta il rate limit → swallow + DB write comunque.
  - `test_abort_tolerates_missing_db_pool` — solo comment, niente DB, no errori.
- **`test_config.py` (+2)**: defaults (`floor=100`, `max_wait=60`) e env override (`500` / `30`).

### 2 — Bruno collection verde

Niente endpoint nuovi. Stesso comando di W3-Task2.

### 3 — Smoke E2E con cap basso

Per simulare in locale il rate limit serve un seed manuale del client. Soluzione: monkeypatch del state via REPL.

```bash
# Server up (con DB)
DATABASE_URL=postgresql://postgres:postgres@localhost:5433/pr_review_agent \
GITHUB_RATE_LIMIT_FLOOR=10000 \
GITHUB_RATE_LIMIT_MAX_WAIT_SECONDS=10 \
  uv run uvicorn pr_review_agent.main:app --port 8001
```

Con `GITHUB_RATE_LIMIT_FLOOR=10000`: GitHub torna `x-ratelimit-remaining: 4500` (sotto floor) → al **secondo** webhook (il primo seed-a lo state, il secondo lo legge) il client fa `_maybe_wait_for_reset` e raise se il reset è > 10s.

Triggera due webhook in rapida successione (anche con uno script bash) e nei log dovresti vedere `rate_limit.aborted` warning + nuova riga in `agent_runs` con `status='aborted_rate_limit'`.

Verifica:

```bash
docker compose exec postgres psql -U postgres -d pr_review_agent -c \
"SELECT id, correlation_id, status, tool_calls_used, tokens_input, cost_usd FROM agent_runs WHERE status='aborted_rate_limit' ORDER BY id DESC LIMIT 3;"
```

### 4 — Smoke con `Retry-After` simulato

Più semplice via REPL — costruisci un client con un fake httpx transport che ritorna 403 + `Retry-After: 60`:

```bash
uv run python - <<'PY'
import asyncio, httpx
from datetime import datetime, timedelta, timezone
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pr_review_agent.github.auth import GitHubAppAuth
from pr_review_agent.github.client import GitHubClient
from pr_review_agent.github.exceptions import GitHubRateLimitError

def handler(request):
    if "access_tokens" in request.url.path:
        exp = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        return httpx.Response(201, json={"token":"ghs_x","expires_at":exp})
    return httpx.Response(403, headers={"Retry-After":"60"})

pem = rsa.generate_private_key(65537, 2048).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()

async def main():
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = GitHubAppAuth(app_id=1, private_key=pem, http_client=http)
        client = GitHubClient(auth=auth, http_client=http)
        try:
            await client.post_pr_comment(installation_id=99, repo="x/y", pr_number=1, body="hi")
        except GitHubRateLimitError as e:
            print(f"OK raised: retry_after={e.retry_after_seconds}s")

asyncio.run(main())
PY
```

Atteso: `OK raised: retry_after=60s`.

### 5 — Smoke con header normali

Server avviato senza override (`floor=100`, `max_wait=60`). Webhook normale → response GitHub porta `x-ratelimit-remaining: ~4500` per token App standard. Il client lo memorizza, `_maybe_wait_for_reset` non si attiva mai. Come ai task precedenti — niente rumore.

## Cosa cercare nei log

- `rate_limit.sleeping_until_reset remaining=… wait_seconds=…` (info) — siamo sotto floor ma il reset è vicino, il client aspetta.
- `rate_limit.aborted retry_after_seconds=…` (warning) — il runner ha abortito un run perché il reset è troppo lontano o GitHub ha mandato 403/429 con `Retry-After`.
- Riga `agent_runs.status='aborted_rate_limit'` con `error` `NULL` (per ora). Se vuoi ricostruire il `retry_after`, è nel commento postato sulla PR.

## Limiti dichiarati

- **State per-process, no coordination**. Due worker uvicorn possono pestare prima di accorgersene. Per il volume attuale (1-2 webhook/min) è OK; se in deploy multi-worker vediamo conflict, si aggiunge un store condiviso.
- **`_maybe_wait_for_reset` non vede il primo response**: la primissima call della prima webhook procede sempre (nessuno snapshot ancora). Per un install fresh con limite scaduto, una singola call può andare in 403 prima che il guardrail si attivi. Il `_check_for_rate_limit_error` la copre comunque reattivamente.
- **`_record_rate_limit` ignora errori di parsing** di header malformati. GitHub non manda mai header malformati, ma siamo difensivi.
- **`Retry-After` numerico solo**: GitHub usa secondi interi. Se in futuro mandasse un HTTP-date, lo ignoriamo (`int(retry_after)` raise → settiamo `seconds=None` e l'eccezione viene comunque sollevata, solo senza il numero specifico).
- **Niente token bucket client-side**. La pacing tra chiamate è gestita dal runner (sequenziale per design — il grafo non parallelizza più chiamate sullo stesso GitHub client).

## Riferimenti file

- `src/pr_review_agent/config.py` — `github_rate_limit_floor`, `github_rate_limit_max_wait_seconds`.
- `src/pr_review_agent/github/exceptions.py` — `GitHubRateLimitError`.
- `src/pr_review_agent/github/client.py` — state + `_record_rate_limit` + `_maybe_wait_for_reset` + `_check_for_rate_limit_error`.
- `src/pr_review_agent/agent/runner.py` — try/except + `_abort_for_rate_limit`.
- `src/pr_review_agent/main.py` — passa rate-limit settings al `GitHubClient`.
- `tests/unit/test_github_client.py` — 6 nuovi test.
- `tests/unit/test_runner_rate_limit.py` — 4 nuovi test.
- `tests/unit/test_config.py` — 2 nuovi test.
