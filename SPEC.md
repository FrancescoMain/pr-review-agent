# PR Review Agent — Project Specification

> Documento da usare come briefing completo con Claude Code (o altro coding agent).
> Posiziona questo file alla root del progetto come `SPEC.md` e referenzialo dal `CLAUDE.md`.

---

## 0. Deviazioni dalla versione originale (confermate 2026-05-05)

Modifiche allo SPEC originale, applicate prima dell'inizio dello sviluppo:

1. **LLM provider:** **solo Anthropic** per tutti i nodi LLM. Rimosso ogni riferimento a OpenAI o altri provider come fallback. La libreria `openai` (e simili) non va installata.
2. **Embeddings:** **locali** con `sentence-transformers`, modello **`BAAI/bge-small-en-v1.5`**. Rimosso l'uso di `text-embedding-3-small` di OpenAI. Niente provider cloud per gli embeddings (no OpenAI, no Voyage, no Cohere).

Le sezioni 2 e 4 sono state aggiornate di conseguenza. Tutte le altre sezioni restano valide.

---

## 1. Contesto e obiettivi

Sto costruendo un progetto portfolio per posizionarmi come **AI Agent Developer** sul mercato italiano (consulenza/freelance). Il mio background è React/TypeScript senior (4 anni, enterprise). Voglio un progetto che dimostri competenze concrete su:

- Architetture agentiche reali (non chatbot, non wrapper)
- Tool calling, memoria persistente, guardrails
- Integrazione con sistemi enterprise (GitHub)
- Evaluation, osservabilità, deploy in produzione

Il progetto è il **PR Review Agent**: un agente che fa code review automatica su Pull Request GitHub, esplorando il repo per recuperare contesto, valutando i cambiamenti, e lasciando commenti inline strutturati.

**Non è un linter. Non è un wrapper di GPT-4 sui diff.** È un agente che ragiona sul codice come farebbe un senior developer durante una review.

---

## 2. Stack tecnologico (vincolato)

- **Linguaggio:** Python 3.12+
- **Package manager:** `uv` (no pip, no poetry)
- **Framework agentico:** LangGraph (ultima versione stabile)
- **LLM provider:** Anthropic (Claude) come **unico** provider per i nodi LLM. Nessun fallback OpenAI / altri provider. La dipendenza `openai` (o equivalenti) **non** deve essere installata.
- **Modelli:**
  - Nodi leggeri (triage, critic): Claude Haiku 4.5
  - Nodo principale (reviewer): Claude Sonnet 4.5 (o Opus 4.7 per casi complessi)
- **API framework:** FastAPI (async)
- **Validation/schemas:** Pydantic v2
- **Vector store:** Qdrant (locale via Docker per dev, cloud per prod)
- **Embeddings:** locali via [`sentence-transformers`](https://www.sbert.net/), modello **`BAAI/bge-small-en-v1.5`** (384 dim, ~100MB di pesi al primo download, gira su CPU). Niente embeddings cloud (no OpenAI, no Voyage, no Cohere). Configurabile via env (`EMBEDDINGS_PROVIDER=local`, `EMBEDDINGS_MODEL=BAAI/bge-small-en-v1.5`).
- **Observability:** LangSmith (free tier)
- **Test:** pytest + pytest-asyncio
- **Linting/formatting:** ruff
- **Type checking:** pyright o mypy
- **Deploy target:** Railway o Fly.io (webhook pubblico)
- **Database:** Postgres (per stato persistente, history PR, eval results)

**Vincoli importanti:**
- Tutto async/await, nessun codice sincrono che blocca l'event loop
- Type hints ovunque, validati con strict mode
- Nessuna dipendenza non strettamente necessaria

---

## 3. Architettura agentica (LangGraph)

L'agente è un **grafo di stati** con i seguenti nodi:

```
[webhook] → [triage] → [context_gatherer] → [reviewer] → [critic] → [publisher]
                                  ↑                ↓
                                  └────── retry ←──┘ (se critic boccia)
```

### Nodo 1: Triage
- **Input:** payload webhook PR, diff
- **Output:** `TriageDecision` (Pydantic) con:
  - `change_type`: enum (feature, bugfix, refactor, docs, chore, test)
  - `risk_level`: enum (low, medium, high)
  - `review_depth`: enum (shallow, standard, deep)
  - `should_skip`: bool (es. PR di solo docs minori)
- **Modello:** Haiku (è classificazione semplice)
- **Logica:** se `should_skip=true` il grafo termina con un commento generico tipo "LGTM, no concerns".

### Nodo 2: Context Gatherer
- **Input:** stato attuale + decisione triage
- **Output:** `RepoContext` con file rilevanti, dipendenze, convenzioni progetto
- **Tools disponibili:**
  - `read_file(path: str) -> str`
  - `list_directory(path: str) -> list[str]`
  - `search_code(query: str, file_pattern: str | None) -> list[Match]`
  - `get_pr_diff() -> str`
  - `get_linked_issues() -> list[Issue]`
  - `read_project_conventions() -> str` (cerca CLAUDE.md, AGENTS.md, README, CONTRIBUTING, .editorconfig)
- **Modello:** Sonnet (deve ragionare su cosa cercare)
- **Limite:** max 15 tool call per evitare context bloat e cost overruns

### Nodo 3: Reviewer
- **Input:** stato + RepoContext
- **Output:** `ReviewPlan` con lista di `ReviewComment` strutturati
- **Schema `ReviewComment`:**
  ```python
  class ReviewComment(BaseModel):
      file: str
      line: int
      severity: Literal["blocker", "major", "minor", "nit", "praise"]
      category: Literal["bug", "security", "performance", "style", "design", "test", "docs"]
      message: str  # actionable, max 500 char
      suggestion: str | None  # code suggestion opzionale
      reasoning: str  # perché lo dico (per il critic)
  ```
- **Modello:** Claude Sonnet 4.5 (default) o Opus 4.7 per PR ad alto rischio
- **Constraint:** struttura output con `with_structured_output()` di LangChain

### Nodo 4: Critic (guardrail principale)
- **Input:** ReviewPlan completo
- **Output:** `CriticVerdict` con commenti filtrati + motivazione rifiuti
- **Logica:** un LLM separato valuta ogni commento contro questi criteri:
  - È actionable? (non vago, non "considera di...")
  - È specifico al cambiamento? (non commenta codice non toccato)
  - Non è banale? (non "aggiungi commenti", non "rinomina x")
  - Non è duplicato di altri commenti?
  - Severity coerente con la categoria?
- **Output finale:** lista di commenti approvati. Se < 30% sono approvati, retry del Reviewer con feedback del Critic (max 1 retry).
- **Modello:** Haiku (è valutazione, non generazione)

### Nodo 5: Publisher
- **Input:** lista commenti approvati
- **Azione:** pubblica via GitHub API:
  - Commenti inline (review comment)
  - Un summary comment generale (markdown con tabella severity/categoria)
  - Eventualmente un review verdict (approve / request changes / comment)
- **No LLM:** è solo orchestrazione di chiamate API

### Stato condiviso (TypedDict)
```python
class AgentState(TypedDict):
    pr_number: int
    repo: str
    diff: str
    triage: TriageDecision | None
    context: RepoContext | None
    review_plan: ReviewPlan | None
    critic_verdict: CriticVerdict | None
    final_comments: list[ReviewComment]
    retry_count: int
    tokens_used: dict[str, int]  # per modello
    cost_usd: float
    errors: list[str]
```

---

## 4. Memoria persistente

L'agente deve **ricordare le convenzioni del progetto** tra PR diverse.

**Implementazione:**
- All'install della GitHub App su un repo, viene fatto un **first-time ingest**:
  - Legge `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, `README.md`, `ADR/*`, `docs/conventions.md`
  - Spezza in chunk semantici (max 1000 token)
  - Embedda in locale con `sentence-transformers` (`BAAI/bge-small-en-v1.5`, 384 dim)
  - Salva in Qdrant in una collection `repo:{owner}/{name}:conventions`
- Su ogni PR, il Context Gatherer ha un tool aggiuntivo:
  - `recall_conventions(query: str, k: int = 3) -> list[Chunk]`
- Dopo ogni review pubblicata, salviamo metadata in Postgres:
  - PR number, decisioni prese, commenti approvati/rifiutati dal critic, costi, latency

**Re-indexing:** webhook su push a main → re-ingest delle convenzioni se cambiano i file rilevanti.

---

## 5. GitHub App integration

Questa è la parte più rognosa, fatela bene dal giorno uno.

**Setup richiesto:**
- Crea GitHub App con:
  - Permissions: `pull_requests: write`, `contents: read`, `metadata: read`, `issues: read`
  - Subscribe to events: `pull_request` (opened, synchronize, reopened)
  - Webhook URL: configurabile via env (per dev usa ngrok)
- Webhook signature verification con `X-Hub-Signature-256` (HMAC SHA256)
- Authentication: JWT firmato con private key → installation token (1h TTL, da cachare)

**Endpoint da esporre:**
- `POST /webhook/github` — riceve eventi PR
- `GET /health` — healthcheck
- `GET /metrics` — Prometheus-style metrics (token usati, latenza, errori)

**Background processing:**
- Il webhook handler **risponde 200 immediatamente** e schedula il lavoro async
- Usa BackgroundTasks di FastAPI per dev, Celery o ARQ per prod

---

## 6. Evaluation

Senza eval il progetto vale metà. Implementare dal giorno uno una mini eval suite.

**Dataset:**
- 20 PR storiche raccolte da repo open source piccoli (es. utility libraries Python/JS)
- Per ognuna: ground truth con problemi noti annotati manualmente

**Metriche:**
- **Precision:** % commenti dell'agente che sono problemi reali
- **Recall:** % problemi reali che l'agente trova
- **False positive rate:** commenti su codice corretto
- **Cost per PR:** USD medio
- **Latency p50/p95:** secondi

**Runner:**
- CLI: `uv run eval --dataset eval/pr_dataset.json --output eval/results.json`
- Output: report markdown con tabella metrics + diff vs run precedenti
- CI: l'eval gira su ogni PR del progetto stesso (meta!)

---

## 7. Observability

- **LangSmith:** trace di ogni run del grafo, configurato via env
- **Logging strutturato:** `structlog` con JSON output, correlation ID per ogni PR processata
- **Cost tracking:** ogni chiamata LLM logga tokens + costo, salvato in Postgres
- **Dashboard minimale:** endpoint `/admin/runs` che mostra ultime 50 PR processate con stato, costo, latenza

---

## 8. Struttura del progetto

```
pr-review-agent/
├── pyproject.toml          # uv-managed
├── README.md               # docs principale
├── CLAUDE.md               # istruzioni per Claude Code (sintesi di SPEC.md)
├── SPEC.md                 # questo documento
├── .env.example            # template env vars
├── docker-compose.yml      # postgres + qdrant locali
├── src/
│   ├── pr_review_agent/
│   │   ├── __init__.py
│   │   ├── main.py         # FastAPI app
│   │   ├── config.py       # Pydantic Settings
│   │   ├── webhook.py      # GitHub webhook handler
│   │   ├── github/
│   │   │   ├── client.py   # GitHub API wrapper
│   │   │   ├── auth.py     # JWT + installation token
│   │   │   └── models.py   # Pydantic models GitHub
│   │   ├── agent/
│   │   │   ├── graph.py    # LangGraph definition
│   │   │   ├── state.py    # AgentState TypedDict
│   │   │   ├── nodes/
│   │   │   │   ├── triage.py
│   │   │   │   ├── context_gatherer.py
│   │   │   │   ├── reviewer.py
│   │   │   │   ├── critic.py
│   │   │   │   └── publisher.py
│   │   │   ├── tools/
│   │   │   │   ├── repo_tools.py
│   │   │   │   └── memory_tools.py
│   │   │   └── prompts/
│   │   │       ├── triage.md
│   │   │       ├── reviewer.md
│   │   │       └── critic.md
│   │   ├── memory/
│   │   │   ├── qdrant_store.py
│   │   │   └── ingest.py
│   │   ├── db/
│   │   │   ├── models.py   # SQLAlchemy
│   │   │   └── migrations/
│   │   └── observability/
│   │       ├── logging.py
│   │       └── cost_tracker.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── eval/
│   ├── dataset/            # PR di test annotate
│   ├── runner.py
│   └── results/
└── scripts/
    ├── ingest_repo.py      # CLI per first-time ingest
    └── replay_pr.py        # CLI per replay di una PR su dev
```

---

## 9. Roadmap a 4 settimane

### Settimana 1 — Fondamenta e MVP grezzo
- [ ] Setup progetto: `uv init`, struttura cartelle, ruff + pyright config
- [ ] FastAPI scaffold con `/health`, `/webhook/github`
- [ ] GitHub App registrata, webhook signature verification funzionante
- [ ] JWT + installation token con caching
- [ ] LangGraph hello-world: grafo a 2 nodi (triage → publisher) che commenta "Hello from agent" su una PR di test
- [ ] Docker Compose con Postgres e Qdrant
- [ ] LangSmith collegato, primi trace visibili

**Deliverable settimana 1:** il bot risponde a una PR su un repo di test con un commento generico, end-to-end funziona.

### Settimana 2 — Tool calling e context gathering
- [ ] Implementare i tool: `read_file`, `list_directory`, `search_code`, `get_pr_diff`, `get_linked_issues`
- [ ] Nodo Context Gatherer con tool calling reale (LangGraph `ToolNode`)
- [ ] Nodo Reviewer con structured output (Pydantic + `with_structured_output`)
- [ ] Pubblicazione di commenti inline reali su PR (non più placeholder)
- [ ] Logging strutturato con correlation ID
- [ ] Cost tracking in Postgres

**Deliverable settimana 2:** il bot fa review reali su PR, con commenti inline pertinenti. Nessun guardrail ancora.

### Settimana 3 — Memoria, critic, guardrails
- [ ] Ingest iniziale convenzioni progetto in Qdrant
- [ ] Tool `recall_conventions` disponibile al Context Gatherer
- [ ] Nodo Critic implementato con retry logic
- [ ] Rate limiting su GitHub API (rispetta header `X-RateLimit-*`)
- [ ] Cost cap configurabile per PR (es. max $0.50, sopra abort con commento)
- [ ] Test su 5-10 PR reali su repo diversi

**Deliverable settimana 3:** sistema robusto, con memory e critic, testato su casi reali.

### Settimana 4 — Evaluation, polish, demo
- [ ] Mini eval suite con 20 PR annotate
- [ ] Runner CLI per eval, output markdown
- [ ] Deploy su Railway o Fly.io con webhook pubblico
- [ ] README serio: architettura, demo GIF, costi medi, limiti noti
- [ ] Demo video di 90 secondi
- [ ] (Bonus) Esposizione come MCP server

**Deliverable settimana 4:** progetto presentabile, deployato, con numeri.

---

## 10. Convenzioni di sviluppo

**Per Claude Code:**

- **Sempre TDD su logica business:** scrivi test prima del codice per nodi del grafo e tool. Non per boilerplate (FastAPI routes).
- **Commit atomici:** un commit = un cambiamento logico. Messaggi in formato Conventional Commits.
- **Branch per feature:** mai push diretti su main. PR self-review prima del merge.
- **No magic:** ogni decisione architetturale documentata con un ADR breve in `docs/adr/`.
- **Type strictness:** pyright in strict mode. No `Any` se non documentato perché.
- **Async-first:** usa `httpx` (non `requests`), `asyncpg` (non `psycopg2`).
- **Errori espliciti:** custom exception per ogni dominio (`GitHubAPIError`, `LLMTimeoutError`, ecc.). Mai `except Exception:`.
- **Secret management:** mai hardcoded, sempre via Pydantic Settings + `.env`.
- **Prompts in file separati:** i prompt LLM sono in `prompts/*.md`, caricati a runtime, versionati.

**Anti-pattern da evitare:**
- ❌ Wrappare l'LLM in un `try/except` che ignora errori
- ❌ Salvare embeddings senza un piano di re-indexing
- ❌ Costruire prompt con f-string complesse inline (usa template)
- ❌ Chiamare il modello "grosso" per task banali (cost overrun)
- ❌ Aggiungere dipendenze "perché potrebbe servire"

---

## 11. Come lavoriamo insieme (workflow con Claude Code)

1. **Inizia leggendo questo SPEC.md per intero.** Non saltare sezioni.
2. **Crea un `CLAUDE.md` alla root** con la sintesi delle convenzioni (sezione 10) + reference a SPEC.md per i dettagli.
3. **Pianifica per task:** prima di scrivere codice, presentami un piano della task corrente con file da toccare e test da scrivere.
4. **Una task alla volta:** non saltare avanti nella roadmap. Completa Settimana 1 prima di toccare Settimana 2.
5. **Al termine di ogni task:** fai recap di cosa è stato fatto, cosa manca, e quale è il prossimo step suggerito.
6. **Quando sei incerto, chiedi.** Meglio una domanda in più che una decisione architetturale sbagliata.
7. **Per ogni componente nuovo:** scrivi un mini-doc (3-5 righe) in cima al file che spieghi cosa fa e perché esiste.

---

## 12. Output atteso alla fine del progetto

Un repo GitHub pubblico con:
- ✅ Codice production-grade in Python (async, typed, testato, ~3000-5000 righe)
- ✅ Architettura agentica reale documentata con diagramma
- ✅ Integrazione GitHub completa (webhook, auth, comments)
- ✅ Memoria persistente con vector store
- ✅ Evaluation quantitativa su 20 PR di test
- ✅ Deploy pubblico funzionante
- ✅ README con demo GIF di 30s
- ✅ Demo video di 90s
- ✅ Costi medi per PR documentati
- ✅ Una riga sul CV: *"PR Review Agent — agente LangGraph con tool calling, memory persistente e critic loop, integrato con GitHub via webhook, deployato in produzione, X% precision su 20 PR di test"*

---

## 13. Punti aperti / decisioni rimandate

Lascia questi aperti, li affronteremo al momento giusto:

- Multi-tenancy: configurazione per repo via `.pr-reviewer.yaml` (settimana 4 bonus)
- Supporto a linguaggi non-Python/JS (TS, Go, Rust): prima fai funzionare bene Python+JS
- Fine-tuning del Reviewer: probabilmente non necessario, il prompt engineering basta
- Multi-LLM routing: per ora hardcoded per nodo, valuta dopo
- UI web per vedere review storiche: bonus, non bloccante

---

**Inizia da qui:** leggi questo documento, poi propone il piano dettagliato per la **Settimana 1, Task 1** (setup progetto). Aspetta il mio OK prima di scrivere codice.
