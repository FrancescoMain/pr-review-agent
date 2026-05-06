# W2 · Task 1 — Tool GitHub-side (`get_pr_diff`, `get_linked_issues`)

## Cosa è stato consegnato

Le prime due "primitive" che il futuro Context Gatherer userà per leggere informazioni dal mondo GitHub, esposte come **LangChain `BaseTool`** già pronte per `bind_tools(...)` o per un `ToolNode` di LangGraph.

- **`get_pr_diff()`** — chiama `GET /repos/{owner}/{repo}/pulls/{pr}` con `Accept: application/vnd.github.diff` e ritorna il diff unificato come `str`. È il modo più compatto per dare al modello "che cosa è cambiato".
- **`get_linked_issues()`** — fa il parse di `Closes/Fixes/Resolves #N` (case-insensitive, tutte le forme: close/closes/closed/fix/fixes/fixed/resolve/resolves/resolved) sul body della PR, deduplica preservando l'ordine, fetch di ogni issue con `GET /repos/{owner}/{repo}/issues/{n}`. Ritorna `list[LinkedIssue]`. **404 silenzioso** (issue cancellata / mai esistita) loggato come `linked_issue.skipped_404` e saltato.
- **Factory `make_github_tools(ctx, client, pr_body_provider)`** — incolla il `PRContext` (repo + pr_number + installation_id) in closure, così il modello non può scegliere a quale PR/repo accedere. Il body del PR è passato come callable per leggerlo dallo state al momento della chiamata, non al momento della build.
- **Nuova eccezione `GitHubNotFoundError`** sotto `GitHubAPIError`, così il tool può catturare *solo* il 404 senza ingoiare 401/403/5xx.
- **Test gap chiuso (memoria progetto):** nuovo test `tests/unit/test_triage_prompt_real.py` che mette in catena il `ChatPromptTemplate` reale (caricato da `triage.md`) con un fake chat model. Catturerebbe in CI la regressione del bug delle graffe nel prompt template di W1.

**Cosa NON è ancora stato fatto:**

- I tool filesystem-side (`read_file`, `list_directory`, `search_code`) — W2 Task 2.
- Il Context Gatherer come nodo del grafo che effettivamente *consuma* i tool — W2 Task 2.
- Inline comment publishing — W2 Task 3.
- Il body è ancora letto via `pr_body_provider` callable: in W2 Task 2 verrà legato direttamente all'`AgentState` quando il Context Gatherer dispatcherà i tool.

## Setup dell'ambiente di test

Nessuna dipendenza nuova: `respx` era già nelle dev deps. Niente env extra rispetto a Task 7 di W1. I test sono completamente offline (`respx` mocka tutto, JWT e installation-token compresi).

## Scenari da testare manualmente

### 1 — Pytest verde

```bash
uv run pytest -q
```

Atteso: **53 passed** (39 di W1 + 11 nuovi `test_github_tools.py` + 3 nuovi `test_triage_prompt_real.py`).

I nuovi sono raggruppati così:

- **Parser regex (`parse_linked_issue_numbers`)** — 4 test: tutte le keyword di chiusura, dedup ordinato, body `None` / vuoto, `#N` non preceduti da una keyword non vengono raccolti.
- **`get_pr_diff`** — 2 test: ritorno del diff unificato (asserisce header `Accept: application/vnd.github.diff` e `Authorization: token …`); 503 → `GitHubAPIError`.
- **`get_linked_issues`** — 4 test: happy path con due issue, 404 silenzioso (issue saltata, lista non vuota), body senza closing keyword (lista vuota, **nessuna** chiamata HTTP), 5xx propagato come `GitHubAPIError`.
- **Client `get_issue`** — 1 test: payload parsato in `dict[str, object]`.
- **Triage prompt reale** — 3 test: `input_variables` esattamente `{title, body}`; chain con body contenente `{"foo": {"bar": 1}}` non esplode; chain senza `body` solleva `KeyError`.

### 2 — Bruno collection verde (sanity check)

Questa task non aggiunge endpoint HTTP, ma controlliamo che la collection resti verde dopo i refactor del client:

```bash
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  uv run uvicorn pr_review_agent.main:app --port 8001 &
until curl -sf http://localhost:8001/health > /dev/null; do sleep 0.2; done
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  bash -lc 'cd bruno && npx --yes @usebruno/cli run --env local'
kill %1
```

Atteso: stesso risultato di W1 Task 7 (12 request, tutti i contratti di health/webhook intatti).

### 3 — Smoke isolato dei tool via REPL

Con un `httpx.MockTransport` non serve internet né credenziali GitHub: si vede subito quale URL e quale `Accept` viene chiamato.

```bash
uv run python - <<'PY'
import asyncio, httpx
from datetime import datetime, timedelta, UTC
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pr_review_agent.agent.tools import make_github_tools, PRContext
from pr_review_agent.github.auth import GitHubAppAuth
from pr_review_agent.github.client import GitHubClient

DIFF = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-old\n+new\n"
ISSUE = {"number": 7, "title": "first issue", "state": "open", "body": ""}

def handler(request: httpx.Request) -> httpx.Response:
    print(">>", request.method, request.url.path, "Accept=", request.headers.get("Accept"))
    if "access_tokens" in request.url.path:
        exp = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        return httpx.Response(201, json={"token": "ghs_x", "expires_at": exp})
    if "/pulls/" in request.url.path:
        return httpx.Response(200, text=DIFF)
    if "/issues/7" in request.url.path:
        return httpx.Response(200, json=ISSUE)
    return httpx.Response(404, json={"message": "not found"})

pem = rsa.generate_private_key(65537, 2048).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()

async def main():
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = GitHubAppAuth(app_id=1, private_key=pem, http_client=http)
        client = GitHubClient(auth=auth, http_client=http)
        ctx = PRContext(repo="francesco/playground", pr_number=42, installation_id=99)
        tools = make_github_tools(
            ctx=ctx, client=client, pr_body_provider=lambda: "Closes #7, fixes #999"
        )
        diff_tool, issues_tool = tools
        print("DIFF:", (await diff_tool.ainvoke({}))[:60], "...")
        print("ISSUES:", await issues_tool.ainvoke({}))

asyncio.run(main())
PY
```

Cosa aspettarsi:

- Una `POST .../access_tokens` (token exchange).
- Una `GET /repos/.../pulls/42` con `Accept=application/vnd.github.diff`.
- Una `GET /repos/.../issues/7` (200) e una `GET /repos/.../issues/999` (404, **non stampata** ma nei log `structlog` come `linked_issue.skipped_404`).
- Risultato finale: il diff e una lista di un `LinkedIssue` (issue 7), niente eccezioni.

### 4 — Verifica errore: 5xx sul diff → `GitHubAPIError`

Cambia `httpx.Response(200, text=DIFF)` in `httpx.Response(503)` nello snippet di §3. Atteso: il `await diff_tool.ainvoke({})` solleva `GitHubAPIError("failed to fetch diff for francesco/playground#42: HTTP 503")`. Il tool **non** mangia l'errore — è il Context Gatherer (W2 Task 2) che deciderà come reagire.

### 5 — Verifica errore: prompt template con graffa non escapata

Per simulare la regressione W1, in `triage.md` aggiungi temporaneamente una riga `Esempio: {change_type}` e lancia:

```bash
uv run pytest tests/unit/test_triage_prompt_real.py -v
```

Atteso: **fail** del test `test_triage_prompt_only_has_title_and_body_variables` con un messaggio chiaro che la lista delle variabili contiene `change_type` di troppo. Rimuovi la modifica per tornare verde.

## Cosa cercare nei log

Quando una linked issue non esiste, structlog emette:

```
linked_issue.skipped_404 repo=… issue_number=…
```

Niente di rilevante sul happy path: i tool sono silenziosi. La verbosità arriverà col Context Gatherer (correlation ID + cost tracking, W2 Task 4-6).

## Limiti dichiarati

- Il parser di `Closes #N` riconosce **solo riferimenti same-repo**. `Closes owner/repo#N` o riferimenti via URL completo non sono ancora gestiti — se in W3 troveremo falsi negativi sul corpus di eval, valutiamo il passaggio a GraphQL `closingIssuesReferences`.
- `get_linked_issues` esegue le fetch in **sequenza**, non in parallelo. Per ora basta: gli issue linkati a una PR sono tipicamente 1-3. Se in W3 vediamo PR con liste lunghe, passiamo a `asyncio.gather`.
- I tool non implementano ancora **rate-limit awareness**. È in roadmap a W3 e verrà gestito a livello di `GitHubClient` (un solo punto), non per-tool.

## Riferimenti file

- `src/pr_review_agent/agent/tools/models.py` — `PRContext`, `LinkedIssue`.
- `src/pr_review_agent/agent/tools/github_tools.py` — factory + parser regex.
- `src/pr_review_agent/agent/tools/__init__.py` — export pubblico.
- `src/pr_review_agent/github/client.py` — nuovi verbi `get_pr_diff`, `get_issue`.
- `src/pr_review_agent/github/exceptions.py` — `GitHubNotFoundError`.
- `tests/unit/test_github_tools.py` — 11 test (parser + tool + client).
- `tests/unit/test_triage_prompt_real.py` — 3 test regressivi sul prompt template.
