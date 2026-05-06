# W3 · Task 5 — Tool `recall_conventions`

## Cosa è stato consegnato

Il **read side** della memoria Qdrant: un sesto tool esposto al Context Gatherer che fa similarity search sulle convenzioni del repo. Quando il modello pensa "ma il progetto ha una regola su X?", chiama `recall_conventions(query)` e riceve i 3-5 chunk più simili. Il Reviewer downstream applica le regole del progetto invece di consigli generici.

- **`ConventionMatch` Pydantic** in `agent/memory/store.py`: `path`, `chunk_index`, `text`, `score` (cosine in [0, 1]).
- **`ConventionStore.query_conventions(repo, query, top_k=5)`**:
  - embed la query via lo stesso `Embedder` usato per l'ingest,
  - chiama `client.query_points(collection, query=vector, limit=top_k, with_payload=True)`,
  - cattura `UnexpectedResponse` / `ValueError` (collection missing, repo mai ingestato) e ritorna **lista vuota** loggando `convention_recall.collection_missing` — graceful fallback per repo non seedati.
  - Query vuota o whitespace → lista vuota, niente chiamata a Qdrant.
- **`make_convention_tools(store, repo, top_k)`** in `agent/tools/convention_tools.py`: factory mirror di `make_github_tools`. Ritorna lista di un singolo `BaseTool`. `repo` e `top_k` sono in closure → l'LLM vede solo `query: str` nello schema.
- **Settings**: `convention_recall_top_k: int = 5` configurabile via env.
- **Wiring**:
  - `runner.make_default_runner` accetta `convention_store: ConventionStore | None = None` e `convention_recall_top_k: int = 5`.
  - Se store presente: `gatherer.repo_tools = [...github..., ...filesystem..., recall_conventions]` (6 tool).
  - Se store assente: i tool restano 5. Niente errori, solo il gatherer ha meno opzioni.
- **`main.py` lifespan**: se `QDRANT_URL` settato, costruisce `AsyncQdrantClient` + `ConventionStore` + lo passa al runner. Cleanup `qdrant_client.close()` in shutdown. Se Qdrant fallisce all'init, log + procede senza convention store.
- **Prompt del Gatherer aggiornato**: ora descrive `recall_conventions` con guidance "use this BEFORE making style assumptions" + "empty result means no specific guidance — fall back to general best practices".

**Cosa NON è stato fatto:**

- Score threshold (filtro su `score < 0.5`). Per ora il modello vede tutti i top_k e decide. In W3-Task7 (eval) vedremo se serve.
- Caching delle query (LRU). Volumi attuali troppo bassi per giustificarlo.
- Rerank con un secondo modello. La similarity di bge-small è già buona per il caso d'uso.
- Auto-trigger ingest sul primo PR di un repo non seedato → W4 deploy.

## Setup dell'ambiente di test

I test pytest sono **completamente offline**: `AsyncQdrantClient(":memory:")` + fake embedder. Smoke E2E richiede Qdrant up + ingest fatto su un repo reale.

## Scenari da testare manualmente

### 1 — Pytest verde

```bash
uv run pytest -q
```

Atteso: **191 passed** (182 dopo W3-Task4 + 4 nuovi `test_convention_store.py` (`query_*`) + 4 nuovi `test_convention_tools.py` + 1 nuovo `test_node_context_gatherer.py` (`recall_conventions integration`)).

I nuovi test:

- **`test_convention_store.py` (+3)**: `query_returns_matches_in_score_order`, `query_returns_empty_for_unknown_repo`, `query_returns_empty_for_empty_query`.
- **`test_convention_tools.py` (4)**: factory espone un singolo tool; il tool chiama lo store con `repo`/`top_k` da closure, query da arg; empty results → empty list; il tool schema espone **solo** `query` (non `repo` né `top_k`).
- **`test_node_context_gatherer.py` (+1)**: scripted LLM chiama `recall_conventions` → tool message JSON contiene il `text` dei match → final_answer chiude il loop.
- **`test_config.py` (+1)**: default `convention_recall_top_k=5` + env override.

### 2 — Bruno collection verde (sanity check)

Niente endpoint nuovi.

### 3 — Smoke E2E completo

**Prerequisiti:**
- Qdrant up: `docker compose up -d qdrant`.
- Postgres up: `docker compose up -d postgres`.
- Ingest del repo playground:
  ```bash
  QDRANT_URL=http://localhost:6333 \
    uv run python -m pr_review_agent.scripts.ingest_conventions \
      --repo FrancescoMain/pr-review-agent-playground \
      --head-sha <ultimo head di main> \
      --installation-id <id>
  ```
- Server avviato:
  ```bash
  DATABASE_URL=postgresql://postgres:postgres@localhost:5433/pr_review_agent \
  QDRANT_URL=http://localhost:6333 \
    uv run uvicorn pr_review_agent.main:app --port 8001
  ```

Atteso log al boot:

```
[info ] convention store ready url=http://localhost:6333
```

Apri/sincronizza una PR sul playground. Nei log del run dovresti vedere il Gatherer chiamare `recall_conventions` (visibile in LangSmith trace) e i match nel ToolMessage. Il commento di review può citare le convenzioni:

> Per le dipendenze il progetto usa `uv` (vedi CLAUDE.md), invece in questo PR è stato modificato direttamente `requirements.txt` — convertilo a `pyproject.toml`.

### 4 — Smoke senza convention store

Stessi step ma senza `QDRANT_URL`:

```bash
unset QDRANT_URL
DATABASE_URL=postgresql://postgres:postgres@localhost:5433/pr_review_agent \
  uv run uvicorn pr_review_agent.main:app --port 8001
```

Atteso log: `QDRANT_URL not set: recall_conventions tool will not be exposed`. Il run procede; il gatherer ha 5 tool, non 6. Nessun errore.

### 5 — Repo non seedato

Configurazione completa di §3 ma triggera una PR su un repo che NON hai mai ingestato (es. crea uno nuovo). Il modello chiama `recall_conventions` → la collection non esiste → Qdrant ritorna `404` → il store logga `convention_recall.collection_missing` e ritorna `[]` → il tool message è `[]` → il gatherer prosegue senza convention guidance.

Verifica nei log: `convention_recall.collection_missing collection=conventions_xxx` come warning, mai come error.

### 6 — Recall isolato via REPL

```bash
uv run python - <<'PY'
import asyncio
from qdrant_client import AsyncQdrantClient
from pr_review_agent.agent.memory.embedder import Embedder
from pr_review_agent.agent.memory.store import ConventionStore, ConventionDocument

class Fake:
    def encode(self, texts, **kw):
        return [[float(i+1) for _ in range(4)] for i, _ in enumerate(texts)]

async def main():
    qdrant = AsyncQdrantClient(":memory:")
    store = ConventionStore(client=qdrant, embedder=Embedder(model=Fake()), vector_size=4)
    repo = "demo/repo"
    await store.recreate_collection(repo)
    await store.upsert_chunks(
        repo=repo, head_sha="abc",
        documents=[
            ConventionDocument(path="CLAUDE.md", chunk_index=0, text="use uv for deps"),
            ConventionDocument(path="CLAUDE.md", chunk_index=1, text="conventional commits"),
            ConventionDocument(path="README.md", chunk_index=0, text="hello world"),
        ],
    )
    matches = await store.query_conventions(repo=repo, query="how do I commit?", top_k=2)
    for m in matches:
        print(f"{m.path}#{m.chunk_index} ({m.score:.3f}): {m.text}")

asyncio.run(main())
PY
```

Atteso 2 match con score in [0, 1].

## Cosa cercare nei log

- `convention store ready url=…` (info, boot) — store inizializzato OK.
- `QDRANT_URL not set: recall_conventions tool will not be exposed` (warning, boot) — funzionamento degraded ma intenzionale.
- `failed to initialise Qdrant client; convention recall disabled` (error, boot) — Qdrant unreachable, fallback graceful.
- `convention_recall.collection_missing collection=… repo=…` (warning, runtime) — il repo non è stato ingestato, il tool ha ritornato [] al modello.

## Limiti dichiarati

- **No rerank, no threshold**: il modello vede tutti i top_k incluso il poco rilevante. Mitigazione futura.
- **`recall_conventions` carico per nodo Gatherer solo**: Reviewer (Sonnet/Opus) NON ha accesso diretto al tool, riceve solo quello che il Gatherer ha messo in `gathered_context.notes` o `relevant_files`. Coerente con la separazione di ruoli (Gatherer raccoglie, Reviewer analizza).
- **Stessa Embedder instance** condivisa tra ingest CLI e runtime. Modello caricato lazy → primo recall in produzione paga ~3s di load. Se in W4 deploy vediamo first-request latency, monteremo il cache HF.
- **Niente filtri payload**: tutta la collection del repo è eligible. Se in futuro ingestassimo *anche* sorgenti, dovremmo filtrare per `path NOT LIKE 'src/%'`.
- **Top_k hardcoded nel runner**, non per-PR. In W3-Task7 (eval) potremmo decidere di alzare per PR `risk=high`.

## Riferimenti file

- `src/pr_review_agent/agent/memory/store.py` — `ConventionMatch`, `query_conventions`.
- `src/pr_review_agent/agent/tools/convention_tools.py` — `make_convention_tools`.
- `src/pr_review_agent/agent/tools/__init__.py` — export.
- `src/pr_review_agent/agent/runner.py` — convention_store + extension di `repo_tools`.
- `src/pr_review_agent/main.py` — lifespan: AsyncQdrantClient + ConventionStore + cleanup.
- `src/pr_review_agent/agent/prompts/context_gatherer.md` — guidance per il modello.
- `src/pr_review_agent/config.py` — `convention_recall_top_k`.
- `tests/unit/test_convention_store.py` — 3 nuovi test (query path).
- `tests/unit/test_convention_tools.py` — 4 nuovi test.
- `tests/unit/test_node_context_gatherer.py` — 1 nuovo test (recall integration).
- `tests/unit/test_config.py` — 1 nuovo test (top_k).
