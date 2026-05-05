# CLAUDE.md — PR Review Agent

Briefing operativo per Claude Code. Sintesi delle convenzioni di sviluppo. Per ogni dettaglio architetturale, decisione di prodotto o roadmap consulta i documenti di riferimento.

## Documenti di riferimento

- **[SPEC.md](./SPEC.md)** — specifica completa del progetto: contesto, architettura LangGraph, schemi dati, memoria, integrazione GitHub, evaluation, observability, struttura cartelle, roadmap a 4 settimane.
- **[SETUP.md](./SETUP.md)** — setup environment (uv, GitHub App, ngrok, API keys, `.env.example`). Già completato da Francesco.

Leggi entrambi prima di iniziare una nuova area del progetto.

## Deviazioni chiave dallo SPEC originale

1. **Solo Anthropic** per i nodi LLM (Haiku 4.5 / Sonnet 4.5 / Opus 4.7). **Mai** installare `openai` o altri provider LLM.
2. **Embeddings locali** con `sentence-transformers`, modello **`BAAI/bge-small-en-v1.5`** (384 dim). Mai embeddings cloud.

## Convenzioni di sviluppo (sintesi SPEC §10)

- **TDD su logica business:** test prima del codice per nodi del grafo e tool. Non per boilerplate (FastAPI routes).
- **Commit atomici:** un commit = un cambiamento logico. Conventional Commits.
- **Branch per feature:** mai push diretti su main. PR self-review prima del merge.
- **No magic:** ogni decisione architetturale documentata con un ADR breve in `docs/adr/`.
- **Type strictness:** pyright in **strict mode**. No `Any` se non documentato perché.
- **Async-first:** `httpx` (non `requests`), `asyncpg` (non `psycopg2`). Niente sync che blocca l'event loop.
- **Errori espliciti:** custom exception per ogni dominio (`GitHubAPIError`, `LLMTimeoutError`, ecc.). **Mai** `except Exception:`.
- **Secret management:** mai hardcoded. Sempre via Pydantic Settings + `.env` (gitignored).
- **Prompts in file separati:** prompt LLM in `prompts/*.md`, caricati a runtime, versionati.
- **Mini-doc per componente:** ogni nuovo file ha 3-5 righe in cima che spiegano cosa fa e perché esiste.

## Anti-pattern da evitare

- ❌ Wrappare l'LLM in `try/except` che ingoia errori
- ❌ Salvare embeddings senza un piano di re-indexing
- ❌ Costruire prompt con f-string complesse inline (usa template)
- ❌ Chiamare il modello "grosso" per task banali (cost overrun)
- ❌ Aggiungere dipendenze "perché potrebbe servire"
- ❌ Introdurre `openai` / `voyageai` / `cohere` come dipendenze (vedi deviazioni)

## API testing — Bruno collection (standard di sviluppo)

Ogni task che introduce o modifica un endpoint HTTP, un webhook, o un flusso end-to-end **deve** aggiornare la Bruno collection in `bruno/`. La collection è strumento di sviluppo (debug manuale + documentazione viva) **e** di automazione: dopo ogni task, `npx @usebruno/cli run --env local bruno/` deve restare verde.

Struttura:

```
bruno/
├── bruno.json                    # collection config
├── environments/
│   └── local.bru                 # baseUrl=http://localhost:8000
├── <area>/                       # es. health/, webhook/, agent/
│   └── <verb-resource>.bru       # es. get-health.bru, post-webhook-stub.bru
└── e2e/
    └── NN-<scenario>.bru         # sequenze multi-request
```

Convenzioni per i `.bru`:

- **Asserzioni minime, non duplicate dai pytest:** Bruno copre il contratto HTTP esterno (status, shape JSON essenziale) e i flussi e2e. Pytest copre la logica interna. Se un check è già in pytest unit, non riscriverlo in Bruno e viceversa.
- **Variabili ambiente:** mai hardcodare URL o secret. Usa `{{baseUrl}}`, `{{webhookSecret}}`, ecc. dichiarate in `bruno/environments/local.bru`.
- **Niente segreti reali nei file `.bru`:** placeholder o `{{ENV_VAR}}` da iniettare. Le credenziali vere vivono in `.env`, gitignored.

Esecuzione richiesta nei comandi di verifica di ogni task con server-up:

```bash
# Avvia il server in background. Default 8001 perché 8000 è spesso
# occupata da altri servizi locali su questa macchina (cambia in
# bruno/environments/local.bru se la tua è libera).
uv run uvicorn pr_review_agent.main:app --port 8001 &
until curl -sf http://localhost:8001/health > /dev/null; do sleep 0.2; done
(cd bruno && npx --yes @usebruno/cli run --env local)
kill %1
```

Richiede Node.js 18+ sulla macchina. Bruno CLI va lanciata dalla cartella che contiene `bruno.json` (la root della collection): senza argomento posizionale scansiona automaticamente tutte le sottocartelle, quindi non serve manutenere una whitelist quando aggiungi nuove aree.

## Workflow con Francesco (SPEC §11)

1. **Pianifica per task** prima di scrivere codice: file da toccare, test da scrivere, comando di verifica. **Aspetta OK esplicito** prima di iniziare.
2. **Una task alla volta**, in ordine di roadmap. Completa Settimana 1 prima di Settimana 2.
3. **Recap a fine task:** cosa fatto, cosa manca, prossimo step suggerito.
4. **Se incerto, chiedi.** Meglio una domanda in più che una decisione architetturale sbagliata.

## Stack — quick reference

Python 3.12+ · `uv` · LangGraph · FastAPI async · Pydantic v2 · `langchain-anthropic` · Qdrant · Postgres (asyncpg / SQLAlchemy) · `sentence-transformers` · LangSmith · structlog · pytest + pytest-asyncio · ruff · pyright (strict).
