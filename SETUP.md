# PR Review Agent — Setup Environment

> Guida step-by-step da completare **prima** di passare lo SPEC.md a Claude Code.
> Tempo stimato: 60-90 minuti la prima volta.
>
> Tutto quello che fai qui ti servirà già pronto quando inizi a lavorare con Claude Code in Settimana 1.

---

## Pre-requisiti di sistema

Verifica di avere installato sulla tua macchina (Windows con WSL2 raccomandato, oppure macOS):

- **Python 3.12+** — `python3 --version`
- **Git** — `git --version`
- **Docker Desktop** — `docker --version` e `docker compose version`
- **GitHub CLI** (opzionale ma comodo) — `gh --version`
- **Editor:** VS Code (raccomandato) con estensioni Python, Pylance, Ruff
- **Account GitHub** personale o organizzazione su cui creare la GitHub App

Se sei su Windows, fai tutto dentro WSL2 (Ubuntu). Niente Python su Windows nativo per questo progetto.

---

## 1. Installazione `uv`

`uv` è il package manager Python che useremo. Sostituisce pip + venv + poetry tutto in uno.

**macOS / Linux / WSL:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Riavvia il terminale e verifica:
```bash
uv --version
```

Dovresti vedere qualcosa tipo `uv 0.5.x`.

---

## 2. API Keys da procurarti

Ti serve **una sola** API key obbligatoria per i nodi LLM (Anthropic). Le altre sono per servizi accessori.

### 2.1 Anthropic API key (obbligatoria)

1. Vai su <https://console.anthropic.com/>
2. Crea un account se non ce l'hai (la registrazione richiede SMS verification)
3. Aggiungi un metodo di pagamento (parti con $5-10 di credito, basta e avanza per le 4 settimane)
4. Vai in **Settings → API Keys → Create Key**
5. Nome consigliato: `pr-review-agent-dev`
6. Copia la key (formato `sk-ant-api03-...`) e salvala in un password manager **subito** (non la rivedrai più)

**Stima costi:** con i modelli giusti (Haiku per triage/critic, Sonnet per reviewer), una review di una PR media costa $0.05-0.20. Per le 4 settimane di sviluppo + eval ti aspetti $15-30 di spesa totale.

### 2.2 Embeddings — decisione importante

Lo SPEC originale prevedeva embeddings OpenAI (`text-embedding-3-small`). Dato che vuoi solo Anthropic, hai **tre opzioni**:

#### Opzione A: Embeddings locali con `sentence-transformers` (raccomandata)

Niente API key esterna, gira tutto in locale. Modello consigliato: `BAAI/bge-small-en-v1.5` (384 dim, multilingua decente, veloce su CPU).

**Pro:** zero costi, zero latency di rete, privacy totale
**Contro:** quality leggermente inferiore agli embeddings cloud, primo download ~100MB
**Verdict:** perfetto per questo use case (convenzioni progetto, non semantic search ad alta precisione)

Se scegli questa, la dipendenza è `sentence-transformers` e basta. Va aggiornato lo SPEC dicendo a Claude Code di usare questo invece di OpenAI.

#### Opzione B: Voyage AI (quality top, alternativa pulita a OpenAI)

Voyage è il provider che Anthropic stessa raccomanda per gli embeddings. Modello: `voyage-3-lite` ($0.02 per 1M token).

**Pro:** quality alta, super economico, ben integrato
**Contro:** un'altra API key da gestire
**Setup:** <https://www.voyageai.com/> → registrati → crea API key. Hanno $50 di crediti gratis al signup.

#### Opzione C: Cohere (terza alternativa)

`embed-multilingual-light-v3.0`, free tier generoso (1000 calls/min).

**Pro:** free tier sufficiente per dev, multilingua nativo
**Contro:** un'altra dipendenza
**Setup:** <https://cohere.com/> → free trial key

**Mio consiglio:** vai con **Opzione A (locale)**. Per il portfolio piece non aggiungere complessità che non sia visibile nel risultato finale. Se in futuro vuoi mostrare anche skill su embeddings cloud, switcha a Voyage in 10 minuti.

### 2.3 LangSmith (free tier, fortemente raccomandata)

LangSmith è il tool di observability per LangGraph. Senza, fai debug a occhi bendati.

1. Vai su <https://smith.langchain.com/>
2. Sign up con account GitHub
3. Crea un nuovo progetto: `pr-review-agent`
4. Vai in **Settings → API Keys → Create API Key**
5. Salva la key (formato `lsv2_pt_...`)

Free tier: 5000 trace/mese, sufficiente per le 4 settimane.

### 2.4 Railway o Fly.io (per deploy, settimana 4)

Non serve subito. Quando arrivi alla settimana 4 scegli uno:
- **Railway** (<https://railway.app>) — più semplice, $5/mese starter
- **Fly.io** (<https://fly.io>) — più potente, free tier generoso

Per ora salta.

---

## 3. Setup GitHub App

Questa è la parte più rognosa. Falla con calma.

### 3.1 Creazione GitHub App

1. Vai su <https://github.com/settings/apps> (o se sei in un'organizzazione: `https://github.com/organizations/YOUR_ORG/settings/apps`)
2. Click **New GitHub App**
3. Compila:
   - **GitHub App name:** `pr-review-agent-dev-francesco` (deve essere globalmente unico)
   - **Homepage URL:** può essere il tuo profilo GitHub
   - **Webhook URL:** **per ora metti** `https://example.com/webhook` (lo aggiorneremo dopo con ngrok)
   - **Webhook secret:** genera con `openssl rand -hex 32` e salvalo
4. **Repository permissions:**
   - `Pull requests`: **Read & write**
   - `Contents`: **Read-only**
   - `Metadata`: **Read-only** (è di default)
   - `Issues`: **Read-only**
5. **Subscribe to events:**
   - ✅ Pull request
6. **Where can this GitHub App be installed?** → **Only on this account**
7. Click **Create GitHub App**

### 3.2 Genera private key

Dopo la creazione, nella pagina dell'app:
1. Scorri fino a **Private keys**
2. Click **Generate a private key**
3. Scarica il file `.pem` e salvalo in `~/.secrets/pr-review-agent.pem` (o path equivalente, **fuori dal repo**)
4. Annota anche l'**App ID** (in alto nella pagina, formato numerico)

### 3.3 Installa l'app su un repo di test

1. Crea un repo nuovo su GitHub: `pr-review-agent-playground` (può essere privato)
2. Clone in locale, aggiungi un README, fai un primo commit
3. Nella pagina della tua GitHub App: **Install App** (sidebar sinistra)
4. Seleziona il tuo account → **Only select repositories** → scegli `pr-review-agent-playground`
5. **Install**

Da ora questo repo manda eventi al tuo webhook (che però è ancora `example.com`, quindi i payload si perdono — è normale per ora).

---

## 4. ngrok per il webhook locale

Durante lo sviluppo, GitHub deve poter raggiungere il tuo laptop. Useremo ngrok.

### 4.1 Installa ngrok

```bash
# macOS
brew install ngrok

# WSL/Linux
curl -s https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | sudo tee /etc/apt/sources.list.d/ngrok.list
sudo apt update && sudo apt install ngrok
```

### 4.2 Account e auth

1. Registrati su <https://ngrok.com/> (free)
2. Copia il tuo authtoken da <https://dashboard.ngrok.com/get-started/your-authtoken>
3. Configura:
   ```bash
   ngrok config add-authtoken YOUR_TOKEN_HERE
   ```

### 4.3 Static domain (gratis con account)

ngrok ti dà un dominio statico gratuito. Vai su <https://dashboard.ngrok.com/cloud-edge/domains> e creane uno (es. `francesco-pr-agent.ngrok-free.app`). Annotalo.

In settimana 1 lo lancerai così:
```bash
ngrok http --url=francesco-pr-agent.ngrok-free.app 8000
```

E aggiornerai il **Webhook URL** della GitHub App a `https://francesco-pr-agent.ngrok-free.app/webhook/github`.

---

## 5. File `.env.example` da creare nel progetto

Quando inizi con Claude Code, fagli creare questo file alla root come template. Le credenziali vere andranno in `.env` (gitignored).

```bash
# === LLM Provider ===
ANTHROPIC_API_KEY=sk-ant-api03-...

# === Embeddings (Opzione A: locale) ===
EMBEDDINGS_PROVIDER=local
EMBEDDINGS_MODEL=BAAI/bge-small-en-v1.5

# === Observability ===
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_pt_...
LANGSMITH_PROJECT=pr-review-agent

# === GitHub App ===
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY_PATH=/home/francesco/.secrets/pr-review-agent.pem
GITHUB_WEBHOOK_SECRET=hex32_string_from_openssl

# === Database ===
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/pr_review_agent
QDRANT_URL=http://localhost:6333

# === App config ===
ENVIRONMENT=development
LOG_LEVEL=INFO
COST_CAP_PER_PR_USD=0.50
MAX_TOOL_CALLS_PER_NODE=15
```

---

## 6. Check finale prima di partire

Prima di lanciare Claude Code con lo SPEC, verifica di avere:

- [ ] `uv --version` funziona
- [ ] `docker compose version` funziona
- [ ] **API key Anthropic** in password manager
- [ ] **API key LangSmith** in password manager
- [ ] **GitHub App** creata, App ID annotato
- [ ] **Private key `.pem`** salvata fuori dal repo
- [ ] **Webhook secret** annotato
- [ ] **Repo playground** creato e GitHub App installata su quel repo
- [ ] **ngrok** installato, authtoken configurato, dominio statico riservato
- [ ] **Editor** con estensioni Python configurate
- [ ] Cartella vuota dove ospiterai il progetto, es. `~/projects/pr-review-agent/`

---

## 7. Modifiche da comunicare a Claude Code

Quando passi lo SPEC a Claude Code, **menziona esplicitamente** queste deviazioni dalla versione originale:

> *"Lo SPEC menzionava OpenAI come fallback LLM e per gli embeddings. Ho deciso di:*
> *1. Usare solo Anthropic per i nodi LLM (no fallback OpenAI)*
> *2. Usare embeddings locali con sentence-transformers (modello `BAAI/bge-small-en-v1.5`) invece di OpenAI*
>
> *Aggiorna SPEC.md di conseguenza prima di iniziare la Settimana 1."*

Questo evita che Claude Code aggiunga dipendenze OpenAI inutilmente.

---

## 8. Comando di avvio per Claude Code

Una volta che tutto sopra è pronto, apri un terminale nella cartella vuota del progetto e lancia Claude Code. Il primo messaggio da inviare è:

```
Sono Francesco. Sto costruendo il PR Review Agent come progetto portfolio
per posizionarmi come AI Agent Developer.

Allegato trovi:
- SPEC.md: specifica completa del progetto
- SETUP.md: setup environment già completato

DEVIAZIONI DALLO SPEC:
1. Solo Anthropic per i nodi LLM (rimuovi ogni riferimento a OpenAI come provider LLM)
2. Embeddings locali con sentence-transformers (modello BAAI/bge-small-en-v1.5),
   non OpenAI

ISTRUZIONI INIZIALI:
1. Leggi SPEC.md per intero, poi SETUP.md
2. Aggiorna SPEC.md con le due deviazioni sopra
3. Crea CLAUDE.md alla root con la sintesi delle convenzioni di sviluppo
   (sezione 10 dello SPEC) + reference a SPEC.md e SETUP.md per i dettagli
4. Presentami il piano dettagliato per la Settimana 1, Task 1 (setup progetto):
   - Quali file creerai
   - Quali dipendenze installerai
   - Quali test scriverai
   - Quale comando dovrò lanciare per verificare che funziona

NON scrivere codice finché non ti do il via libera sul piano.
```

---

## 9. Domande frequenti

**Devo fare tutto questo prima di iniziare?**
Sì. Se salti il setup di GitHub App e ngrok, in Settimana 1 ti incarti per ore su roba che non è il cuore del progetto.

**Posso usare un LLM diverso da Anthropic?**
Per imparare LangGraph va bene anche Ollama locale, ma per il portfolio piece Anthropic è il signal giusto da dare al mercato (è il framework più rappresentativo del 2026 e Claude è top-tier per code review).

**E se mi blocco sul GitHub App?**
È normale, è la parte meno divertente. Quando ti incarti, chiedi a Claude Code di farti debug guidato sul webhook signature verification — è uno dei punti dove ti perdi più facilmente.

**Quanto mi viene a costare in totale?**
Anthropic API: $15-30 per le 4 settimane di dev + eval. Tutto il resto: gratis (free tier o locale).

**Devo per forza usare WSL2 su Windows?**
Per sanità mentale, sì. Docker, Python con dipendenze native, signal handling — tutto funziona meglio su Linux. WSL2 è ufficialmente supportato e l'esperienza è quasi nativa.

---

**Quando hai completato la checklist della sezione 6, sei pronto per partire con Claude Code.**
