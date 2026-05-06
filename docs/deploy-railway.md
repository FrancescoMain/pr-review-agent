# Deploy guide — Railway + Qdrant Cloud

Step-by-step per portare il PR Review Agent in produzione su Railway, con Postgres managed da Railway e Qdrant cloud-managed (free tier). Tempo stimato: **30 minuti**, di cui ~10 di click sulle GUI e 5-10 di propagazione DNS / build iniziale.

Stack risultante:

```
GitHub webhook ─┐
                ├─→ Railway (FastAPI service)
                │     ├─→ Railway Postgres (managed)
                │     ├─→ Qdrant Cloud (free tier, 1GB)
                │     ├─→ Anthropic API
                │     └─→ LangSmith
```

Costi mensili attesi: **~$10/mese** (Railway service ~$5 + Postgres ~$5) + spend Anthropic per-PR.

---

## 0. Prerequisiti

- [ ] Account Railway con metodo di pagamento (anche solo per le linee di credito gratuite — non ti addebitano se resti sotto $5/mo).
- [ ] Account Qdrant Cloud (`cloud.qdrant.io`).
- [ ] Una GitHub App esistente e funzionante in locale (vedi `SETUP.md`). Tieni a portata di mano: **App ID**, il file `.pem` della private key, **webhook secret**.
- [ ] La tua `ANTHROPIC_API_KEY` e (opzionali) credenziali LangSmith.

---

## 1. Crea il cluster Qdrant Cloud

1. Vai su [cloud.qdrant.io](https://cloud.qdrant.io) → "Sign up" → connect con GitHub.
2. "Create cluster" → seleziona il **free tier** (`1GB`, single node, ~9k vettori 384-dim ≈ abbondanti per le convenzioni). Region: la più vicina a Railway us-east o eu-west — coerente con il dyno Railway che sceglierai.
3. Una volta creato (~2 min), prendi nota di:
   - **Cluster URL** (es. `https://abc123.eu-west-1-0.aws.cloud.qdrant.io:6333`)
   - **API Key** (genera una "Read+Write" key dal pannello "API keys").
4. Test rapido da terminale:
   ```bash
   curl -H "api-key: <key>" "<cluster-url>/healthz"
   ```
   Atteso: `healthz check passed`.

---

## 2. Crea il progetto Railway

1. Vai su [railway.app](https://railway.app) → "Start a new project" → **Deploy from GitHub repo** → autorizza l'organizzazione GitHub e scegli `FrancescoMain/pr-review-agent`.
2. Railway auto-rileva il `Dockerfile` e parte col primo build. Lascialo lavorare; in parallelo aggiungi il DB.

---

## 3. Aggiungi Postgres managed

1. Nella dashboard del progetto Railway → **+ New** → **Database** → **PostgreSQL**.
2. Railway crea un servizio Postgres e iniett tre env var nel servizio dell'app:
   - `DATABASE_URL` (formato `postgresql://user:pass@host:port/dbname`)
   - `PGHOST`, `PGPORT`, ecc. (ignoriamo, basta `DATABASE_URL`).
3. **Importante**: Railway usa di default il driver `postgresql://` standard (compatibile con asyncpg). Se vedi `postgresql+asyncpg://` nel `.env.example`, in produzione **lascia solo `postgresql://`** — asyncpg gestisce il driver automaticamente.

---

## 4. Imposta le env var del servizio FastAPI

Dashboard → seleziona il servizio dell'app → tab **Variables**. Aggiungi:

### Critiche

| Nome | Valore | Note |
|---|---|---|
| `ENVIRONMENT` | `production` | abilita il validator strict di Settings |
| `LOG_LEVEL` | `INFO` | |
| `GITHUB_APP_ID` | (il tuo App ID) | da GitHub App settings |
| `GITHUB_APP_PRIVATE_KEY_PEM` | (vedi sotto) | il PEM **inline**, multi-linea |
| `GITHUB_WEBHOOK_SECRET` | (il tuo webhook secret) | quello settato nella GitHub App |
| `ANTHROPIC_API_KEY` | `sk-ant-…` | |
| `DATABASE_URL` | (auto-iniettata da Railway) | non toccare |
| `QDRANT_URL` | (URL del cluster Qdrant) | dal §1 |
| `QDRANT_API_KEY` | (API key del cluster) | dal §1 |

### Opzionali

| Nome | Valore | Note |
|---|---|---|
| `LANGSMITH_TRACING` | `true` | se vuoi i trace |
| `LANGSMITH_API_KEY` | `lsv2_pt_…` | |
| `LANGSMITH_PROJECT` | `pr-review-agent-prod` | progetto dedicato per separare prod/dev |
| `COST_CAP_PER_PR_USD` | `0.50` | default OK; alza se PR grandi |
| `CONVENTION_RECALL_TOP_K` | `5` | |
| `GITHUB_RATE_LIMIT_FLOOR` | `100` | default OK |
| `GITHUB_DEFAULT_INSTALLATION_ID` | (per eval, opzionale) | |

### Come incollare il PEM inline

Il `.pem` ha più righe. Railway accetta env var multi-linea: nel campo **Value**, incolla il file `.pem` **incluse** le righe `-----BEGIN/END-----`. Esempio:

```
-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEA...
...
-----END RSA PRIVATE KEY-----
```

Il `Settings.resolve_github_app_private_key()` (vedi `src/pr_review_agent/config.py`) preferisce `PEM` inline a `PATH`: in produzione setta solo `PEM`, lascia `GITHUB_APP_PRIVATE_KEY_PATH` non settato.

> ⚠️ **Mai** committare il PEM o un `.env` con il PEM nel repo. `.gitignore` esclude già `.env` e `*.pem`; il `.dockerignore` riconferma.

---

## 5. Volume per la cache HF (sentence-transformers)

Il modello `BAAI/bge-small-en-v1.5` pesa ~130 MB. Senza un volume persistente, ogni redeploy lo riscarica.

1. Dashboard → servizio app → tab **Volumes** → **Add volume**.
2. Mount path: `/data` (combaciante con `HF_HOME=/data/hf-cache` nel Dockerfile).
3. Size: `1 GB` è abbondante.

Costo: ~$0.25/mese al GB. Senza volume, primo recall_conventions del freshly deployed worker prende +5-10s di download.

---

## 6. Aggiorna la GitHub App webhook URL

1. Una volta che Railway mostra "deployed" e il servizio risponde su un dominio tipo `https://pr-review-agent-production.up.railway.app`:
   ```bash
   curl -sf https://pr-review-agent-production.up.railway.app/health && echo " ok"
   ```
2. GitHub App settings → **Webhook** → **Webhook URL**: incolla `https://pr-review-agent-production.up.railway.app/webhook/github`. Salva.
3. GitHub manda automaticamente un `ping` event al nuovo URL: dovrebbe dare HTTP 202 con `{"status":"ignored","reason":"event not handled: ping"}`. Verifica nel pannello GitHub App → **Recent Deliveries** che il `ping` sia stato accettato.

---

## 7. Smoke test

1. Apri o spingi un commit su una PR di un repo dove la tua GitHub App è installata.
2. Dashboard Railway → **Logs** del servizio app: vedi i log strutturati JSON con `correlation_id`.
3. Su LangSmith, project `pr-review-agent-prod`: vedi il trace con i 5 nodi (triage → gatherer → reviewer → critic → publisher).
4. Sulla PR di GitHub: vedi una review reale postata dal bot in ~60s.
5. Verifica DB:
   ```bash
   railway run psql $DATABASE_URL -c "SELECT id, correlation_id, status, cost_usd FROM agent_runs ORDER BY id DESC LIMIT 3;"
   ```
   *(`railway run` esegue un comando con le env del servizio; alternativa: connect via TablePlus/DBeaver con la connection string copiata da Railway.)*

---

## 8. Auto-deploy on push to `main`

Railway è già configurato per auto-redeploy quando spingi a `main`. Per usarlo come safety net:

- Tieni i feature branch (`feat/*`) come adesso.
- Mergia su `main` solo dopo che la branch è verde su CI (ruff + pyright + pytest).
- Railway intercetta il push, builda il Dockerfile, applica le migration al boot, sostituisce il container vecchio con uno nuovo (zero-downtime se hai >1 replica).

Per disabilitare l'auto-deploy temporaneamente: dashboard → **Settings** → **Service** → **Auto Deploy** → toggle off.

---

## 9. Costi attesi & monitoring

| Risorsa | Costo | Note |
|---|---|---|
| Railway service (shared CPU) | ~$5/mese | scale to zero non disponibile su shared, ma traffic sotto il free di $5 = nessun addebito reale |
| Railway Postgres (1 GB) | ~$5/mese | il volume si paga separato |
| Volume HF cache (1 GB) | ~$0.25/mese | |
| Qdrant Cloud free tier | $0 | 1 GB cluster, sufficient per 5-10 repo seedati |
| Anthropic | per-uso | ~$0.085/PR media = $25 ogni 300 PR |

Monitor:
- **Railway Metrics** tab → CPU/RAM del servizio (sentence-transformers in memoria pesa ~600MB).
- **LangSmith** → spesa Anthropic, traces.
- **Postgres `agent_runs`** → costi totali per run aggregati con SQL:
  ```sql
  SELECT date_trunc('day', started_at) AS day,
         count(*),
         sum(cost_usd) AS total_usd
  FROM agent_runs
  GROUP BY day
  ORDER BY day DESC;
  ```

---

## 10. Troubleshooting comune

**Build fallisce su uv install** → controlla che `uv.lock` sia committato al repo. Railway non auto-installa uv senza il binary; il Dockerfile lo prende da `ghcr.io/astral-sh/uv`.

**`agent run failed` con `RepoCloneError`** → il container ha `git`? Sì, è nel Dockerfile runtime. Se manca, ricontrolla che il `RUN apt-get install -y --no-install-recommends git` sia rimasto.

**`DATABASE_URL` con prefisso `postgresql+asyncpg://`** → Railway lo da come `postgresql://` (corretto). Se in qualche shell hai impostato il prefix asyncpg, **rimuovilo** (asyncpg gestisce in autonomia).

**`webhook signature invalid`** → mismatch tra `GITHUB_WEBHOOK_SECRET` su Railway e quello nella GitHub App. Forza un re-set dell'env var su Railway con il secret esatto della App, poi triggera un re-deploy.

**Qdrant connection refused** → controllo l'API key (Read+Write, non solo Read) e che l'URL includa `:6333`. Test:
```bash
curl -H "api-key: $QDRANT_API_KEY" "$QDRANT_URL/collections"
```

**Cold start lento (>30s) sul primo webhook dopo redeploy** → è il download del modello sentence-transformers in `/data/hf-cache`. Successivi sono <1s. Se è bloccante in produzione, considera un init script che pre-cacha il modello al boot.

---

## 11. Rollback

Railway tiene la cronologia dei deployment. Per rollback rapido:

1. Dashboard → **Deployments** → trova l'ultimo deployment verde.
2. **... → Redeploy**.

Container precedente in <30s. Se il bug è in DB / migration, rollback DB richiede backup point-in-time (Railway lo fa daily).

---

## 12. Quando finisci

- [ ] Update `README.md` (sezione "Deployed at") con l'URL pubblico, una volta confermato che funziona.
- [ ] Update `README.it.md` con la stessa info.
- [ ] Compila la sezione "Deploy" dello SPEC.md §8 se necessario.
- [ ] Disable il server locale uvicorn (Shell A) — non serve più.
- [ ] Spegni `ngrok` localmente.
- [ ] Annuncia sul tuo profilo / LinkedIn col link al deploy + repo (post-demo video).
