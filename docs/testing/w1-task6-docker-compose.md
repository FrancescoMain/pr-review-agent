# W1 · Task 6 — Docker Compose (Postgres + Qdrant)

## Cosa è stato consegnato

Un `docker-compose.yml` alla root che alza i due servizi di stato persistente che useremo da W2 in poi:

- **Postgres 17 (alpine)** — esposto su host port **5433** (non 5432, perché 5432 è già occupata sulla tua macchina da `pinkcare-db`). DB pre-creato `pr_review_agent`, user/pass `postgres/postgres`. Persistenza in `./postgres_data/`.
- **Qdrant v1.13.0** — esposto su 6333 (HTTP) e 6334 (gRPC). Persistenza in `./qdrant_data/`.

Entrambi con healthcheck (`pg_isready` per Postgres, TCP probe via `/dev/tcp` per Qdrant — l'image qdrant non ha `wget`/`curl`).

`DATABASE_URL` in `.env.example` (e nel tuo `.env`) ora punta a `postgresql+asyncpg://postgres:postgres@localhost:5433/pr_review_agent`. `QDRANT_URL` resta `http://localhost:6333`.

**Cosa NON è ancora stato fatto:**
- Connessione lato app (asyncpg pool / Qdrant client) — Task 6 alza l'infrastruttura, l'app **non** la usa ancora.
- Schema tabelle / migrations Alembic — W2-W3, quando aggiungeremo cost tracking e PR history.
- Ingest delle convenzioni progetto in Qdrant — W3.

## Setup dell'ambiente di test

Prerequisito: Docker Desktop installato + integrazione WSL attiva (verificato: `docker --version` → 29.2.1, `docker compose version` → v5.1.0).

## Scenari da testare manualmente

### 1 — Avvio

```bash
docker compose up -d
docker compose ps
```

Atteso: due container partono, dopo ~10 s entrambi `healthy`.

```
NAME                 STATUS                       PORTS
pr-review-postgres   Up XX seconds (healthy)      0.0.0.0:5433->5432/tcp
pr-review-qdrant     Up XX seconds (healthy)      0.0.0.0:6333-6334->6333-6334/tcp
```

Se Qdrant resta `health: starting` per più di 30 s, il TCP probe non sta passando (`docker compose logs qdrant`).

### 2 — Postgres connectivity

```bash
docker compose exec postgres psql -U postgres -d pr_review_agent -c '\l'
```

Atteso: lista dei database, con `pr_review_agent` presente. Per connetterti dall'host:

```bash
PGPASSWORD=postgres psql -h localhost -p 5433 -U postgres -d pr_review_agent -c 'select 1;'
```

### 3 — Qdrant connectivity

```bash
curl -sS http://localhost:6333/collections
```

Atteso: `{"result":{"collections":[]},"status":"ok","time":...}`.

### 4 — Bruno smoke

Con uvicorn anche up:

```bash
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  uv run uvicorn pr_review_agent.main:app --port 8001 &
until curl -sf http://localhost:8001/health > /dev/null; do sleep 0.2; done
(cd bruno && GITHUB_WEBHOOK_SECRET=test-bruno-secret npx --yes @usebruno/cli run --env local)
kill %1
```

Atteso: **13 request, 26/26 assert verdi**. La nuova `qdrant/get-collections.bru` parla direttamente a Qdrant.

### 5 — Persistenza Qdrant

```bash
curl -sS -X PUT http://localhost:6333/collections/test_persist \
  -H 'Content-Type: application/json' \
  -d '{"vectors": {"size": 4, "distance": "Cosine"}}'
docker compose restart qdrant
sleep 8
curl -sS http://localhost:6333/collections    # test_persist deve essere ancora lì
curl -sS -X DELETE http://localhost:6333/collections/test_persist
```

### 6 — Stop / reset

Stop (dati conservati): `docker compose down`.
Reset totale: `docker compose down && rm -rf postgres_data qdrant_data`.

## Cosa cercare nei log

```bash
docker compose logs --tail=20 postgres   # "database system is ready to accept connections"
docker compose logs --tail=20 qdrant     # "Qdrant HTTP listening on 6333"
```

Niente `ERROR` / `PANIC`.

## Limiti dichiarati

- **Niente migrations Alembic.** Le tabelle (run history, cost tracking, eval) arrivano in W2 con la prima migration.
- **Niente client lato app.** L'app non importa `asyncpg` né `qdrant_client`. La prima integrazione vera è il cost-tracker in W2.
- **Porta 5433 invece di 5432.** Perché 5432 è occupata da `pinkcare-db`. Se la liberi, cambia il mapping in `docker-compose.yml` e `DATABASE_URL` nel `.env`.
- **Healthcheck Qdrant via TCP, non HTTP.** Il container Qdrant non porta `wget`/`curl`. Quando in W3 wireremo il client Python, l'errore "non c'è" verrà subito evidenziato dal client al primo tentativo.

## Riferimenti file

- `docker-compose.yml`
- `.env.example`, `.env` — `DATABASE_URL` aggiornato (5433)
- `bruno/qdrant/get-collections.bru`
- `bruno/environments/local.bru` — `+qdrantUrl`
- `.gitignore` — `postgres_data/` e `qdrant_data/` già esclusi
