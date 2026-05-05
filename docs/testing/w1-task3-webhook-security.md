# W1 · Task 3 — Webhook signature verification + payload parsing

## Cosa è stato consegnato

Il webhook `POST /webhook/github` non è più uno stub permissivo. La nuova pipeline:

1. **Verifica firma `X-Hub-Signature-256`** (HMAC-SHA256, confronto constant-time): se mancante o non corrispondente al secret configurato → `401 invalid signature`. Il messaggio è volutamente neutro: non riveliamo se l'header era assente, malformato o semplicemente sbagliato (security best practice).
2. **Parse JSON** del body: se non è valido → `400 body is not valid JSON`.
3. **Routing per evento**: `X-GitHub-Event: pull_request` → validato e accettato; ogni altro evento (`ping`, `installation`, `push`, ecc.) → `202 ignored` con un piccolo envelope. Acknowledge silenzioso così GitHub non re-invia all'infinito.
4. **Validazione Pydantic** del payload `pull_request`: campi obbligatori → `action`, `number`, `pull_request.{number,title,head{ref,sha},base{ref,sha}}`, `repository.full_name`. Mancanti → `422`.
5. **Risposta happy** (`pull_request` valido firmato): `202 {"status":"accepted","pr":<n>,"repo":"owner/repo"}`. Il vero lavoro (review LangGraph) verrà dispatchato in background da Task 4.

**Cosa NON è ancora stato fatto:**
- JWT GitHub App + installation token (Task 4).
- Background dispatch verso il grafo LangGraph (Task 4-5).
- Idempotency su delivery duplicate (rimandato).

## Setup dell'ambiente di test

```bash
uv sync
# .env deve contenere GITHUB_WEBHOOK_SECRET (lo stesso che hai messo nella GitHub App).
# Per i test locali con curl/Bruno useremo un secret di test — non serve quello vero:
export GITHUB_WEBHOOK_SECRET=test-bruno-secret
uv run uvicorn pr_review_agent.main:app --port 8001
```

Nelle prossime due sezioni il secret del server e quello che Bruno legge da `process.env` devono coincidere — sennò ovviamente firme valide diventano "invalide" lato server. Stesso discorso per il test e2e con la GitHub App reale: lì userai il vero secret della tua App.

## Scenari da testare manualmente

### 1 — Happy: `pull_request` firmato correttamente

```bash
SECRET=test-bruno-secret
BODY='{"action":"opened","number":42,"pull_request":{"number":42,"title":"Hello","head":{"ref":"feature/x","sha":"deadbeef"},"base":{"ref":"main","sha":"cafebabe"}},"repository":{"full_name":"francesco/playground"}}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
curl -i -X POST http://localhost:8001/webhook/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: sha256=$SIG" \
  -d "$BODY"
```

Atteso: `202 Accepted`, body `{"status":"accepted","pr":42,"repo":"francesco/playground"}`.

### 2 — Errore: firma mancante

```bash
curl -i -X POST http://localhost:8001/webhook/github \
  -H "Content-Type: application/json" -H "X-GitHub-Event: pull_request" -d '{}'
```

Atteso: `401 Unauthorized`, body `{"detail":"invalid signature"}`. Anche se il body sarebbe `{}` la firma manca → respinto immediatamente, prima del parse.

### 3 — Errore: firma calcolata con un secret diverso

```bash
WRONG=not-the-real-secret; BODY='{}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WRONG" | awk '{print $2}')
curl -i -X POST http://localhost:8001/webhook/github \
  -H "Content-Type: application/json" -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: sha256=$SIG" -d "$BODY"
```

Atteso: `401`. Stesso messaggio del punto 2 — non riveliamo che la firma è "wrong" vs "missing".

### 4 — Errore: body non JSON

```bash
SECRET=test-bruno-secret; BODY='this is not json'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
curl -i -X POST http://localhost:8001/webhook/github \
  -H "Content-Type: application/json" -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: sha256=$SIG" -d "$BODY"
```

Atteso: `400`, body `{"detail":"body is not valid JSON"}`. La firma è valida (così esercitiamo solo il parser, non l'auth).

### 5 — Errore: payload pull_request senza i campi obbligatori

```bash
SECRET=test-bruno-secret; BODY='{"action":"opened"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
curl -i -X POST http://localhost:8001/webhook/github \
  -H "Content-Type: application/json" -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: sha256=$SIG" -d "$BODY"
```

Atteso: `422 Unprocessable Entity`, body con array di errori Pydantic — ogni elemento ha `loc`, `msg`, `type`. Pydantic ti dice esattamente quali campi mancano (`number`, `pull_request`, `repository`).

### 6 — Evento non gestito: `ping`

```bash
SECRET=test-bruno-secret; BODY='{"zen":"Practicality beats purity."}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
curl -i -X POST http://localhost:8001/webhook/github \
  -H "Content-Type: application/json" -H "X-GitHub-Event: ping" \
  -H "X-Hub-Signature-256: sha256=$SIG" -d "$BODY"
```

Atteso: `202 Accepted`, body `{"status":"ignored","reason":"event not handled: ping"}`. Lo stesso vale per qualunque altro `X-GitHub-Event` diverso da `pull_request`.

### 7 — Method not allowed

```bash
curl -i http://localhost:8001/webhook/github
```

Atteso: `405 Method Not Allowed`.

### 8 — Bruno collection (CLI)

```bash
export GITHUB_WEBHOOK_SECRET=test-bruno-secret
cd bruno
npx --yes @usebruno/cli run --env local
```

Atteso: **12 request, 22 assert verdi**. La collection include 4 happy path (2 health + 2 webhook firmati: pull_request e ping) e 8 exception (`health/exceptions/{405-post,404-typo}`, `webhook/exceptions/{405-get,404-typo,401-missing-signature,401-invalid-signature,422-malformed-pull-request}`, `e2e/02-webhook-pull-request`).

I `.bru` del webhook calcolano la firma in pre-script con `crypto-js` leggendo il secret da `bru.getProcessEnv('GITHUB_WEBHOOK_SECRET')`. Il body viene letto da `req.getBody()` e firmato così com'è — questo significa che ogni nuovo `.bru` con body deve seguire lo stesso pattern (vedi `bruno/webhook/post-webhook-pull-request.bru` come template).

### 9 — Test end-to-end con ngrok + GitHub App reale (manuale)

Da fare quando vuoi vedere il flusso vero, non lo script di test:

1. Esporta il secret **reale** della tua GitHub App: `export GITHUB_WEBHOOK_SECRET=<quello-che-hai-messo-nella-App>`
2. Avvia il server: `uv run uvicorn pr_review_agent.main:app --port 8001`
3. In altra shell, espone il server via ngrok: `ngrok http --url=<tuo-dominio-statico> 8001`
4. Sulla pagina **Settings → Webhooks** della GitHub App, aggiorna **Webhook URL** a `https://<tuo-dominio-statico>/webhook/github` e salva (GitHub manda subito un `ping` di verifica).
5. Vai su `https://github.com/settings/apps/<la-tua-app>/advanced` → **Recent Deliveries**: dovresti vedere il `ping` con risposta `202` e il body `{"status":"ignored","reason":"event not handled: ping"}`.
6. Apri o sincronizza una PR sul repo `pr-review-agent-playground` (basta un `git push` con qualche commit su un branch + apertura PR via web).
7. Recent Deliveries → `pull_request` → status `202`, body `{"status":"accepted","pr":<n>,"repo":"..."}`.
8. Nei log di uvicorn dovresti vedere la richiesta arrivare e completarsi velocemente (< 50 ms) — il vero lavoro async non è ancora collegato.

Se vedi `401`: il secret della GitHub App e quello in `GITHUB_WEBHOOK_SECRET` non coincidono.
Se vedi `400` o `422` su un evento `pull_request` reale: il payload di GitHub è cambiato e il nostro Pydantic model va aggiornato.

## Cosa cercare nei log

In Task 3 i log applicativi sono ancora pochi: quello che vedi è soprattutto uvicorn (linea per ogni request). I log di errore di firma non sono espliciti: l'`exception_handler` ritorna direttamente il 401 senza loggare. Quando in W2 aggiungeremo il middleware con correlation-ID, ogni 401/422 verrà loggato con dettagli, qui resta volutamente silenzioso per evitare rumore.

## Scenari di errore NON coperti in Bruno (ma sì in pytest)

- **`400 non-JSON body` con firma valida** — Bruno CLI non permette di inviare un body raw non-JSON insieme a una firma calcolata sullo stesso byte stream in modo affidabile (l'interazione tra `body:text`/`body:none` e `setBody()` lascia il server con body diverso da quello firmato). Il caso è completamente coperto da `tests/unit/test_webhook_security.py::test_non_json_body_returns_400`. Per provarlo a mano c'è lo scenario 4 sopra con `curl`.

## Riferimenti file

Codice consegnato in Task 3:

- `src/pr_review_agent/github/exceptions.py` — `GitHubError`, `WebhookSignatureError`.
- `src/pr_review_agent/github/signatures.py` — `verify_signature` (HMAC-SHA256 constant-time).
- `src/pr_review_agent/github/models.py` — Pydantic models del payload `pull_request`.
- `src/pr_review_agent/webhook.py` — pipeline completa (riscritto rispetto allo stub di Task 2).
- `src/pr_review_agent/main.py` — exception handler `WebhookSignatureError → 401`.
- `src/pr_review_agent/config.py` — campo `github_webhook_secret: SecretStr`.

Test:

- `tests/unit/test_signatures.py` — 6 test sull'helper di firma.
- `tests/unit/test_webhook_security.py` — 7 test end-to-end via TestClient.

Bruno:

- `bruno/webhook/post-webhook-pull-request.bru`, `bruno/webhook/post-webhook-ping.bru` — happy.
- `bruno/webhook/exceptions/{401-missing-signature,401-invalid-signature,422-malformed-pull-request,405-get,404-typo}.bru`.
- `bruno/e2e/02-webhook-pull-request.bru` — sequenza health → webhook firmato.
- `bruno/environments/local.bru` — solo `baseUrl`; il secret arriva da `process.env`.
