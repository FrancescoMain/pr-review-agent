# W2 · Task 2 — Tool filesystem (`read_file`, `list_directory`, `search_code`) + `RepoCheckout`

## Cosa è stato consegnato

I tre tool che permetteranno al Context Gatherer (W2 Task 3) di "navigare" il codice della PR, e l'infrastruttura per **materializzare il head della PR su disco** in modo isolato e a basso costo.

- **`RepoCheckout`** — async context manager che fa `git init` → `git fetch --depth=1 origin <head_sha>` → `git checkout FETCH_HEAD` su una `tempfile.mkdtemp()`. Auth in produzione via installation token nell'URL (`https://x-access-token:<token>@github.com/<repo>.git`). Cleanup `shutil.rmtree` su `__aexit__`, con o senza eccezione. In test passi `remote_url_override=` con un path locale o un `file://` per restare offline.
- **`read_file(path: str) -> str`** — lettura UTF-8, **traversal-safe** (path assoluti e `..` rifiutati con `ToolPathError`), bounded a `MAX_FILE_BYTES = 200 KB` con marker `[... truncated: file is X bytes ...]` in coda. Decoding con `errors="replace"` per non esplodere su binari mascherati da testo.
- **`list_directory(path: str) -> list[str]`** — listing ordinato, directories suffissate con `/`, **skip-dir** hardcoded (`.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `dist`, `build`, `.mypy_cache`), bounded a `MAX_LIST_ENTRIES = 200` con marker di troncamento.
- **`search_code(query: str, file_pattern: str | None = None) -> list[Match]`** — `git grep -n --fixed-strings -e <query> [-- <pattern>]` eseguito in subprocess sulla checkout, parsing `path:line:text`, bounded a `MAX_SEARCH_MATCHES = 50`. `query` vuota → `ToolPathError`. Nessun match → lista vuota (exit code 1 di git grep, gestito).
- **`PRContext`** ora include `head_ref` e `head_sha` — il SHA è l'oggetto chirurgicamente corretto su cui fare review (immune da force-push tra webhook e clone).
- **Nuove eccezioni**: `RepoCloneError` (sotto `GitHubError`) per fallimenti di `git`; `ToolPathError` come dominio a sé per errori di contratto-tool (path escape, file mancante, query vuota). Esponendole come tipi distinti il Context Gatherer potrà reagire in modo diverso (loop di retry sul tool error, abort sul clone error).

**Cosa NON è stato fatto:**

- Il **Context Gatherer** vero (nodo del grafo che chiama i tool con `bind_tools` o `ToolNode`) — quello è W2 Task 3.
- **Inline comment publishing** sul PR — W2 Task 4.
- `make_filesystem_tools` accetta direttamente un `Path`, non `RepoCheckout`. Voluto: meno coupling. Il runner farà `make_filesystem_tools(checkout.root)`.

## Setup dell'ambiente di test

Nessuna dipendenza nuova. Il binario `git` deve essere disponibile sul `PATH` (lo era già — la repo è un git repo). I test usano fixture che fanno `git init` su tmpdir e `subprocess.run`, niente rete, niente token: l'intera suite gira in **~0.3 s** in locale e in CI.

## Scenari da testare manualmente

### 1 — Pytest verde

```bash
uv run pytest -q
```

Atteso: **76 passed** (53 dopo W2 Task 1 + 7 nuovi `test_repo_checkout.py` + 16 nuovi `test_filesystem_tools.py`).

Suddivisione dei nuovi test:

- `test_repo_checkout.py` (7): clone materializza il SHA, cleanup su `__aexit__`, `root` non accessibile fuori dal `with`, `RepoCloneError` su remote inesistente, `RepoCloneError` su SHA sconosciuto, nessun tmpdir lasciato dietro su failure, costruttore valida l'XOR `auth` / `remote_url_override`.
- `test_filesystem_tools.py::read_file` (6): UTF-8 OK, `..` rifiutato, path assoluto rifiutato, file mancante, directory passata come file, troncamento oltre `MAX_FILE_BYTES` con marker.
- `test_filesystem_tools.py::list_directory` (5): root, subdir, `..` rifiutato, dir mancante, cap a `MAX_LIST_ENTRIES` con marker.
- `test_filesystem_tools.py::search_code` (5): match cross-file, no-match → `[]`, `file_pattern='*.py'` filtra correttamente, query vuota → `ToolPathError`, cap a `MAX_SEARCH_MATCHES`.

### 2 — Bruno collection verde (sanity check)

Niente endpoint nuovi, ma controlliamo che la collection non sia stata rotta dai tocchi a `agent/tools/__init__.py` o ai test esistenti:

```bash
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  uv run uvicorn pr_review_agent.main:app --port 8001 &
until curl -sf http://localhost:8001/health > /dev/null; do sleep 0.2; done
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  bash -lc 'cd bruno && npx --yes @usebruno/cli run --env local'
kill %1
```

Atteso: stesso risultato di W2 Task 1.

### 3 — Smoke isolato dei tool su un repo locale via REPL

Niente rete, niente token. Si fa `git init` su `/tmp/xxx`, lo si passa come `remote_url_override`, e poi si esercitano i tool con il `checkout.root`:

```bash
uv run python - <<'PY'
import asyncio, subprocess, tempfile
from pathlib import Path
from pr_review_agent.agent.tools import (
    PRContext, RepoCheckout, make_filesystem_tools,
)

# 1. Setup di un "remote" locale.
remote = Path(tempfile.mkdtemp(prefix="rpr-remote-"))
def git(*args):
    subprocess.run(["git", *args], cwd=remote, check=True, capture_output=True, env={
        "GIT_AUTHOR_NAME":"t","GIT_AUTHOR_EMAIL":"t@x","GIT_COMMITTER_NAME":"t","GIT_COMMITTER_EMAIL":"t@x",
        "GIT_CONFIG_GLOBAL":"/dev/null","GIT_CONFIG_SYSTEM":"/dev/null","PATH":"/usr/bin:/bin"})
git("init","--quiet","--initial-branch=main")
(remote/"README.md").write_text("hello world\n")
(remote/"src").mkdir(); (remote/"src"/"app.py").write_text("def greet():\n    return 'hello world'\n")
git("add","-A"); git("commit","-m","init","--quiet")
git("config","uploadpack.allowAnySHA1InWant","true")
sha = subprocess.run(["git","rev-parse","HEAD"], cwd=remote, capture_output=True, text=True).stdout.strip()
print("remote =", remote, " sha =", sha)

# 2. Checkout + tools.
ctx = PRContext(repo="x/y", pr_number=1, installation_id=99, head_ref="main", head_sha=sha)

async def main():
    async with RepoCheckout(ctx=ctx, remote_url_override=str(remote)) as co:
        print("root =", co.root)
        tools = make_filesystem_tools(co.root)
        rf, ld, sc = (next(t for t in tools if t.name == n) for n in ("read_file","list_directory","search_code"))
        print("LIST .  :", await ld.ainvoke({"path":"."}))
        print("LIST src:", await ld.ainvoke({"path":"src"}))
        print("READ src/app.py:", (await rf.ainvoke({"path":"src/app.py"}))[:60], "…")
        print("GREP 'hello world':", await sc.ainvoke({"query":"hello world"}))
        print("GREP 'nope':", await sc.ainvoke({"query":"nope"}))

asyncio.run(main())
print("(checkout root cleaned up automatically)")
PY
```

Cosa aspettarsi:

- `LIST .` mostra `README.md`, `src/`, **niente** `.git/`.
- `LIST src` mostra `app.py`.
- `READ src/app.py` stampa l'inizio del file.
- `GREP 'hello world'` ritorna **due** `Match` (README + app.py).
- `GREP 'nope'` ritorna `[]`.
- A fine script il tmpdir di `RepoCheckout` non c'è più (il cleanup è già successo all'uscita dal `with`).

### 4 — Verifica errore: path traversal

```bash
uv run python - <<'PY'
import asyncio
from pathlib import Path
from pr_review_agent.agent.tools import make_filesystem_tools
from pr_review_agent.github.exceptions import ToolPathError

async def main():
    tools = make_filesystem_tools(Path("/tmp"))   # no clone needed: /tmp esiste
    rf = next(t for t in tools if t.name == "read_file")
    try:
        await rf.ainvoke({"path":"../etc/passwd"})
    except ToolPathError as e:
        print("OK rejected:", e)

asyncio.run(main())
PY
```

Atteso: `OK rejected: path escapes checkout root: '../etc/passwd'`. Stesso comportamento per `path` assoluto.

### 5 — Verifica errore: clone di un SHA inesistente

Modifica lo snippet §3 sostituendo `head_sha=sha` con `head_sha="0"*40`. Atteso: il `RepoCheckout.__aenter__` solleva `RepoCloneError("git fetch ... failed with exit 128: ...")`. Il tmpdir **non** resta dietro (il `try/except` interno fa `rmtree` prima di rilanciare).

## Cosa cercare nei log

I tool sono silenziosi sul happy path. `RepoCheckout` non logga ancora — il correlation-ID strutturato arriverà nella Task 6 di W2. In errore propaga il messaggio di `git` (sanitizzato: l'URL del remote, che può contenere il token, viene mascherato come `<remote-url>` prima di diventare parte di `RepoCloneError`).

## Limiti dichiarati

- **Bounded reads non sono limiti hard sul costo del modello.** Sono protezioni contro file giganti / directory pazze / grep esplosivi. Il cost cap per PR è W3.
- **`search_code` è solo `--fixed-strings`** (no regex). Voluto: niente injection, comportamento prevedibile. Se in W3 vediamo che il modello chiede regex spesso, valutiamo di esporre un secondo tool `search_code_regex`.
- **`list_directory` non ricorre.** Il modello deve chiamarlo per ogni livello: voluto, mantiene l'output piccolo per prompt e gli costa una tool-call per ogni discesa (tradeoff context vs latency che sceglie il modello, non noi).
- **Cleanup è synchronous** (`shutil.rmtree`) dentro `__aexit__`. Per directory grandi (~MB) è OK; se nei deploy reali vediamo blocking event-loop andremmo su `asyncio.to_thread` ma non vale il rumore ora.
- **No retry sul clone.** Se la rete tossisce all'inizio del run, il run fallisce. Retry policy con backoff è più sensato a livello di Context Gatherer (W2 Task 3) o di runner (W4).

## Riferimenti file

- `src/pr_review_agent/agent/tools/models.py` — `PRContext` esteso, nuovo `Match`.
- `src/pr_review_agent/agent/tools/repo_checkout.py` — async context manager.
- `src/pr_review_agent/agent/tools/filesystem_tools.py` — i tre `@tool` + costanti di bound.
- `src/pr_review_agent/agent/tools/__init__.py` — export pubblico aggiornato.
- `src/pr_review_agent/github/exceptions.py` — `RepoCloneError`, `ToolPathError`.
- `tests/unit/test_repo_checkout.py` — 7 test.
- `tests/unit/test_filesystem_tools.py` — 16 test.
