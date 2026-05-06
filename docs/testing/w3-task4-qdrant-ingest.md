# W3 · Task 4 — Ingest convenzioni in Qdrant

## Cosa è stato consegnato

Lo **scaffolding di memoria** del progetto: prendere i file di convention di un repo (CLAUDE.md, README, CONTRIBUTING…) e indicizzarli in Qdrant come vettori, così che in W3-Task5 il Context Gatherer possa fare similarity search via un tool `recall_conventions`. Questa task fa solo il **lato write**: un CLI che, dato `repo` + `head_sha`, popola la collection.

- **Embedder** (`agent/memory/embedder.py`) — wrapper su `sentence-transformers` pinnato a `BAAI/bge-small-en-v1.5` (384 dim, deviazione CLAUDE.md). **Lazy load** del modello (130 MB di torch weights non vengono scaricati finché non serve davvero); supporta injection di un fake model per test e CI.
- **Chunker** (`agent/memory/chunker.py`) — split su `\n\n`, fallback a sentence split, hard-split su sentence > cap. Default `max_chars=2000` (~500 token, sotto i 512 di context del modello). Niente librerie di chunking.
- **`ConventionStore`** (`agent/memory/store.py`) — orchestra `AsyncQdrantClient` + `Embedder`:
  - `recreate_collection(repo)` — drop + create idempotente. Distance `Cosine`, vector size 384.
  - `upsert_chunks(repo, head_sha, documents)` — embed batch + upsert in un colpo.
  - `count(repo)` — utility per smoke check.
  - Naming: `conventions_<owner>_<name>` (slashes e dashes sostituiti con underscore).
- **CLI** (`scripts/ingest_conventions.py`) — `python -m pr_review_agent.scripts.ingest_conventions --repo … --head-sha … --installation-id …`. Apre `RepoCheckout` (clone shallow su tmpdir), trova file via `Settings.convention_doc_globs`, chunka, embed, recreate+upsert. Idempotente per design (recreate-and-replace).
- **Settings nuovi**:
  - `qdrant_url: str | None = None` — default disabilita ingest. `.env.example` punta a `http://localhost:6333` (docker-compose).
  - `qdrant_api_key: SecretStr` — vuoto in dev, popolato per Qdrant cloud.
  - `convention_doc_globs: list[str]` — default `["CLAUDE.md", "AGENTS.md", "README.md", "README.rst", "CONTRIBUTING.md", ".editorconfig", "docs/**/*.md"]`.
- **Niente integrazione col Gatherer** in questa task — solo l'ingest. La parte read (tool `recall_conventions`) è W3-Task5.

**Cosa NON è stato fatto:**

- Auto-trigger dell'ingest al primo PR di un nuovo repo. Lo aggiungiamo in W4 deploy con un job in coda.
- Ingest incrementale (solo i file cambiati). Recreate-and-replace è abbastanza per il volume previsto (centinaia di chunk per repo).
- Indexing di file `.py` / `.ts` / sorgenti. Solo i doc files. Sorgenti li legge il Gatherer via `read_file` quando servono.
- Filtri / rerank in lettura (W3-Task5).

## Setup dell'ambiente di test

I test pytest sono **completamente offline**: usano `AsyncQdrantClient(":memory:")` e un fake embedder. Niente download del modello, niente container.

Per smoke E2E con Qdrant reale serve il container già avviato in W1-Task6:

```bash
docker compose up -d qdrant
curl -sf http://localhost:6333/healthz && echo " ok"
```

## Scenari da testare manualmente

### 1 — Pytest verde

```bash
uv run pytest -q
```

Atteso: **182 passed** (163 dopo W3-Task3 + 7 chunker + 4 embedder + 4 store + 2 CLI + 2 settings = 19 nuovi).

I nuovi test:

- **`test_chunker.py` (7)**: input vuoto, paragraph split, blank lines multiple, fallback sentence split, hard split di una frase oversized, ordine preservato, whitespace strippato.
- **`test_embedder.py` (4)**: shape corretto con fake model, empty input → empty output, model `model_name` preservato senza load, supporto numpy-like via `.tolist()`.
- **`test_convention_store.py` (4)**: collection_name slug; recreate + upsert + count roundtrip; recreate droppa i punti vecchi; upsert di 0 documenti.
- **`test_ingest_conventions_cli.py` (2)**: pipeline end-to-end via `run_ingest` contro `:memory:` Qdrant + checkout su file:// remote (real `git init`); caso "no file matches".
- **`test_config.py` (+2)**: `qdrant_url`/`qdrant_api_key` default; `convention_doc_globs` defaults sensati.

### 2 — Bruno collection verde (sanity check)

Niente endpoint nuovi. Stesso comando di W3-Task3.

### 3 — Smoke ingest contro Qdrant locale

Server Qdrant up:

```bash
docker compose up -d qdrant
```

CLI con un repo reale (richiede GitHub App configurato e il repo accessibile dall'installation):

```bash
QDRANT_URL=http://localhost:6333 \
GITHUB_APP_ID=<id> \
GITHUB_APP_PRIVATE_KEY_PATH=$HOME/.secrets/pr-review-agent.pem \
  uv run python -m pr_review_agent.scripts.ingest_conventions \
    --repo FrancescoMain/pr-review-agent-playground \
    --head-sha <head_sha> \
    --installation-id <installation_id>
```

Atteso log:
- `convention_store.recreated collection=conventions_FrancescoMain_pr_review_agent_playground`
- `convention_store.upserted count=N`
- `ingest.done count=N`

Verifica direttamente su Qdrant:

```bash
curl -s http://localhost:6333/collections/conventions_FrancescoMain_pr_review_agent_playground | jq '.result | {points_count, vectors_count, config: .config.params.vectors}'
```

Atteso: `points_count` > 0, `config.size: 384`, `distance: "Cosine"`.

Per sbirciare i payload:

```bash
curl -s -X POST http://localhost:6333/collections/conventions_FrancescoMain_pr_review_agent_playground/points/scroll \
  -H 'Content-Type: application/json' \
  -d '{"limit": 3, "with_payload": true, "with_vector": false}' | jq '.result.points[].payload | {path, chunk_index, text}'
```

### 4 — Smoke senza GitHub App (REPL contro `:memory:`)

Stesso pattern dei test, ma manuale per debugging:

```bash
uv run python - <<'PY'
import asyncio, subprocess, tempfile
from pathlib import Path
from qdrant_client import AsyncQdrantClient
from pr_review_agent.agent.memory.embedder import Embedder
from pr_review_agent.agent.memory.store import ConventionStore
from pr_review_agent.agent.tools import PRContext, RepoCheckout
from pr_review_agent.scripts.ingest_conventions import run_ingest

class Fake:
    def encode(self, texts, **kw): return [[0.1]*4 for _ in texts]

# Local "remote"
remote = Path(tempfile.mkdtemp())
def git(*args):
    subprocess.run(["git", *args], cwd=remote, check=True, capture_output=True, env={
        "GIT_AUTHOR_NAME":"t","GIT_AUTHOR_EMAIL":"t@x","GIT_COMMITTER_NAME":"t","GIT_COMMITTER_EMAIL":"t@x",
        "GIT_CONFIG_GLOBAL":"/dev/null","GIT_CONFIG_SYSTEM":"/dev/null","PATH":"/usr/bin:/bin"})
git("init","--quiet","--initial-branch=main")
(remote/"README.md").write_text("# demo\n\nFirst paragraph.\n\nSecond.\n")
(remote/"CLAUDE.md").write_text("# rules\n\nUse uv.\n\nCommits in conventional form.\n")
git("add","-A"); git("commit","-m","i","--quiet")
git("config","uploadpack.allowAnySHA1InWant","true")
sha = subprocess.run(["git","rev-parse","HEAD"], cwd=remote, capture_output=True, text=True).stdout.strip()

async def main():
    qdrant = AsyncQdrantClient(":memory:")
    store = ConventionStore(client=qdrant, embedder=Embedder(model=Fake()), vector_size=4)
    def factory(ctx):
        return RepoCheckout(ctx=ctx, remote_url_override=str(remote))
    n = await run_ingest(
        repo="francesco/playground", head_sha=sha, installation_id=99,
        store=store, checkout_factory=factory,
        globs=["README.md","CLAUDE.md"],
    )
    print("written:", n)
    print("count:", await store.count("francesco/playground"))

asyncio.run(main())
PY
```

Atteso: `written: 4`, `count: 4` (README e CLAUDE hanno 2 paragrafi ciascuno).

### 5 — Verifica error: Qdrant down

Spegni il servizio:

```bash
docker compose stop qdrant
```

Rilancia §3. Atteso: `httpx.ConnectError` propagata fino allo script, exit code 1. Riavvia `docker compose start qdrant`.

### 6 — Verifica error: settings mancanti

```bash
unset QDRANT_URL
uv run python -m pr_review_agent.scripts.ingest_conventions --repo x/y --head-sha 0 --installation-id 1
```

Atteso log `ingest.qdrant_url_missing hint='set QDRANT_URL in .env'`, exit code 1.

## Cosa cercare nei log

- `convention_store.recreated collection=… repo=…` (info) — collection ricreata pulita.
- `convention_store.upserted collection=… count=N head_sha=…` (info) — N chunks indicizzati.
- `ingest.no_matching_files repo=… globs=…` (warning) — i glob non hanno trovato nulla. Verifica i pattern.
- `ingest.skip_non_utf8 path=…` (warning) — file binario o encoding strano. Skipped silenziosamente.
- `ingest.qdrant_url_missing` / `ingest.github_app_credentials_missing` (error) — configurazione incompleta.

## Limiti dichiarati

- **Recreate-and-replace, no incremental**. Per piccoli repo è OK; per repo enormi (centinaia di MB di docs) andrebbe rifatto. Il volume tipico è di pochi KB di docs convention → 5-50 chunks.
- **Modello da 130 MB**. La prima esecuzione del CLI scarica i pesi in `~/.cache/huggingface`. Container/CI ephemerali pagano questo costo a ogni avvio se non hanno il cache montato. Per W4 deploy, montare il cache.
- **Collection per repo** — niente isolation tenant cross-repo. Quando il bot serve molti repo, valuteremo un singolo store con filtri.
- **Niente embedding di sorgenti**. Voluto: il Gatherer ha già `read_file` / `search_code` / `list_directory`. La memoria Qdrant è solo per le **convenzioni**.
- **CLI synchronous-bound a httpx**. Per il volume previsto basta; se in futuro vorremo ingestare 100 repo in parallelo, swap a un pool worker.
- **Lazy load del modello** non protegge i test che girano `Embedder().encode([...])` senza fake. Se in CI vediamo un test che dimentica il `model=fake_model`, scarichiamo 130 MB. Mitigazione: tutti i nuovi test in W3 mettono `model=fake`.

## Riferimenti file

- `src/pr_review_agent/agent/memory/__init__.py` — module docstring.
- `src/pr_review_agent/agent/memory/chunker.py` — `chunk_document`.
- `src/pr_review_agent/agent/memory/embedder.py` — `Embedder`.
- `src/pr_review_agent/agent/memory/store.py` — `ConventionStore`, `ConventionDocument`, `collection_name_for`.
- `src/pr_review_agent/scripts/__init__.py` — module docstring.
- `src/pr_review_agent/scripts/ingest_conventions.py` — CLI + `run_ingest`.
- `src/pr_review_agent/config.py` — `qdrant_url`, `qdrant_api_key`, `convention_doc_globs`.
- `.env.example` — `QDRANT_URL` placeholder + commented `QDRANT_API_KEY`.
- `tests/unit/test_chunker.py`, `test_embedder.py`, `test_convention_store.py`, `test_ingest_conventions_cli.py` — 17 nuovi test.
- `tests/unit/test_config.py` — 2 nuovi test.
