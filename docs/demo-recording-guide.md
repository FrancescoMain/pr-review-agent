# Guida alla registrazione del video demo

Obiettivo: video da **90 secondi** che mostri il PR Review Agent dal commit del developer al commento postato sulla PR. Pubblico target: ricruiter / clienti / community AI Agent Developer.

Tono: tecnico ma accessibile. Pochi tagli. Niente musica di sottofondo (distrae). Voice-over in italiano (versione principale) + sottotitoli inglesi (versione export).

---

## 1. Cosa registrare — la storia in 6 beat

Pensa al video come a 6 sezioni temporali. Ogni beat ~15 secondi:

| # | Durata | Schermo | Voce |
|---|---|---|---|
| 1 | 0:00–0:10 | Title card o GitHub repo | "Questo è un agente che fa code review automatiche su Pull Request GitHub" |
| 2 | 0:10–0:25 | Apri una PR sul playground (commit con bug) | "Quando un developer apre una PR, il bot riceve il webhook" |
| 3 | 0:25–0:50 | Tail dei log uvicorn / LangSmith trace | "Internamente, un grafo LangGraph fa triage, gather, review, critic, publish" |
| 4 | 0:50–1:10 | Tab GitHub "Files changed" della PR con la review postata | "Inline comments anchorati alle linee giuste, con severity e suggerimenti di codice" |
| 5 | 1:10–1:25 | DBeaver o psql con `agent_runs` row | "Tutto è tracciato in Postgres: token, costo per modello, status" |
| 6 | 1:25–1:30 | Outro: link al repo + GitHub handle | "Codice e testing guides aperti su github.com/FrancescoMain/pr-review-agent" |

**Totale: 90s.** Se sfori, taglia il beat 5 (DB) o riduci il 3 (trace).

---

## 2. Setup di registrazione

### Strumenti consigliati (Windows/WSL)

| Strumento | A cosa serve | Note |
|---|---|---|
| **OBS Studio** | Cattura schermo + audio | Gratis, robust. Consigliato per la versione finale. [obsproject.com](https://obsproject.com) |
| **Game Bar (Win+G)** | Cattura veloce per draft | Built-in Windows 11; output `.mp4` in `Videos/Captures/`. Buono per i primi giri. |
| **DaVinci Resolve** (gratis) | Editing + tagli + sottotitoli | Overkill per 90s; usalo se vuoi sottotitoli professionali. |
| **CapCut Desktop** (gratis) | Editing veloce | Più semplice di DaVinci. Generazione automatica sottotitoli. |
| **asciinema** | (Solo per terminale) | Bello per blog ma NON è video — niente GitHub UI. Skip per questa demo. |

### Configurazione OBS minima

1. Scarica OBS Studio da [obsproject.com](https://obsproject.com) (Windows installer).
2. Al primo avvio, scegli "Optimize for recording, not streaming".
3. Settings → **Output**:
   - Recording Path: `C:\Users\cesar\Videos\demo-pra\`
   - Recording Format: `mp4`
   - Encoder: hardware se hai GPU (NVENC/AMF), altrimenti x264.
   - Bitrate: ~6000 kbps (qualità HD per upload YouTube/LinkedIn).
4. Settings → **Video**:
   - Base (canvas) Resolution: la tua nativa (1920×1080 o 2560×1440).
   - Output (scaled): `1920×1080` (anche se il monitor è più alto — meno upload time).
   - FPS: 30.
5. Settings → **Audio** → desktop audio: **Disabled** (non vogliamo notifiche / musica). Mic: il tuo mic da podcast / cuffie.
6. **Sources** in Scene 1:
   - "Display Capture" (tutto lo schermo) **oppure** "Window Capture" specifico (consigliato — niente notifiche random).
   - "Audio Input Capture" (il mic).

### Preparazione delle finestre prima di REC

Apri **a misura** (e tienile pronte sui workspace di Windows):

1. **VS Code** sul progetto — file aperto: `src/pr_review_agent/agent/graph.py`. Mostra brevemente la `build_graph` definition. Tema scuro consigliato.
2. **Browser tab 1** — la PR `https://github.com/FrancescoMain/pr-review-agent-playground/pull/2` (refresh prima del REC perché vogliamo lo stato pulito *prima* del nuovo commit).
3. **Browser tab 2** — LangSmith project view, ordinato per data (run più recente in cima).
4. **Terminal 1 (server uvicorn)** — server già up con `DATABASE_URL=...`. Tail dei log visibile.
5. **Terminal 2 (commit + DB query)** — directory `/tmp/.../pr-review-agent-playground` pronta. Prepara già il comando in clipboard.
6. **DBeaver o psql** già connesso al DB locale, con la query salvata:

   ```sql
   SELECT id, correlation_id, status, tool_calls_used, tokens_input, tokens_output, cost_usd
   FROM agent_runs ORDER BY id DESC LIMIT 1;
   ```

7. **Title card** (optional) — apri una immagine 1920×1080 con il titolo "PR Review Agent — 90s demo" e il tuo handle. Photoshop/Canva/anche PowerPoint Slide.

---

## 3. Script tecnico — comandi e azioni esatte

Le righe **`[VOCE]`** sono il voice-over. Le righe **`[AZIONE]`** sono cosa fai sullo schermo.

### Beat 1 — Title card (0:00–0:10)

[AZIONE] Apri OBS, premi **Start Recording**. Mostra title card per 3-4 secondi, poi salta sul VS Code col file `graph.py`.

[VOCE] *"Ciao, sono Francesco. Vi mostro un agente che fa code review automatiche su Pull Request GitHub. È costruito con LangGraph e Anthropic, ed è un progetto end-to-end di portfolio."*

### Beat 2 — Apri la PR (0:10–0:25)

[AZIONE] Vai su Terminal 2. Esegui:

```bash
cd /tmp/tmp.rG4fooe770/pr-review-agent-playground && \
  git -c user.name=Francesco -c user.email=cesaranofrancescomain@gmail.com \
      commit --allow-empty -m "demo trigger" && git push
```

[VOCE] *"Quando un developer apre una PR — qui simulo con un commit empty — GitHub manda un webhook al nostro server."*

### Beat 3 — Mostra il flusso interno (0:25–0:50)

[AZIONE] Switch su Terminal 1 (server log). Vedrai passare:

```
INFO:     POST /webhook/github HTTP/1.1" 202 Accepted
[info ] convention store ready ...
```

Aspetta che inizino i log strutturati (~3-5s dopo il push). Poi switch su tab LangSmith. Refresh: il run nuovo dovrebbe apparire in cima. Click sul run.

[VOCE] *"Internamente, un grafo a 5 nodi LangGraph: triage classifica la PR, context gatherer esplora il codebase con tool, reviewer produce la review strutturata, critic fa QA, publisher pubblica. Su LangSmith vedo ogni step con i token consumati."*

[AZIONE] Mentre parli, hover sui nodi nel trace: triage → context_gatherer → reviewer → critic → publisher.

### Beat 4 — La review su GitHub (0:50–1:10)

[AZIONE] Switch su Browser tab 1 (PR). Refresh. Scrolla fino alla nuova review con inline comments. Espandi 1-2 inline comment per mostrare body con severity glyph + codice suggerito.

[VOCE] *"E questa è la review postata, con commenti inline ancorati alle righe giuste, severity calibrata, e suggerimenti di codice patch-ready. Il bot ha catturato due bug — il ZeroDivisionError non gestito — e ha proposto i test mancanti."*

### Beat 5 — Tracciamento DB (1:10–1:25)

[AZIONE] Switch su DBeaver. Esegui la query salvata. Mostra la riga risultato con `cost_usd`, `tokens_input`, `tokens_output`.

[VOCE] *"Ogni run è tracciato in Postgres: il costo, i token per modello, lo status. Posso correlarlo al run di LangSmith con il delivery ID di GitHub. Il costo medio è circa **otto centesimi** per PR."*

### Beat 6 — Outro (1:25–1:30)

[AZIONE] Switch a una tab pulita o al README su GitHub. Scrolla brevemente la sezione "Architecture".

[VOCE] *"Codice, testing guide, eval harness e setup completo sono open su github.com/FrancescoMain. Sotto il video trovi i link. Ciao."*

[AZIONE] Premi **Stop Recording** in OBS.

---

## 4. Editing checklist

Carica il file `.mp4` su CapCut Desktop (o DaVinci). Lavora su queste cose, in ordine:

1. **Trim outer silence** — taglia il primo e l'ultimo secondo se hai esitazioni.
2. **Cut dead time** — qualsiasi pausa > 1.5s tra azioni va tagliata. Lascia 0.5s di "respiro" tra beat ma non di più.
3. **Speed up** dei momenti di attesa puramente tecnici (tipo i 30s di run dell'agente) a **2x-3x**. Mantieni l'audio normale parlato.
4. **Zoom in** sui dettagli importanti:
   - L'inline comment con severity glyph (rendi leggibile).
   - La riga del DB con `cost_usd`.
5. **Sottotitoli inglesi** — usa la generazione automatica di CapCut, poi correggi a mano i nomi tecnici (LangGraph, Anthropic, ecc.).
6. **Color/contrast** — solo se il tema VS Code o il browser è troppo scuro o slavato. Niente filter.
7. **Audio normalize** — porta il volume mic a -3dB di picco. Niente musica.

### Esportazione

- **Formato**: `.mp4` H.264, AAC audio.
- **Risoluzione**: `1920×1080` 30fps.
- **Bitrate**: ~8-12 Mbps (qualità alta).
- **Lunghezza**: idealmente **85-90 secondi**, hard cap **95**. Se sei a 100+ rivedi il punto 3 della checklist.

### Due versioni di export

1. **`pra-demo-it.mp4`** — voice-over italiano + sottotitoli italiani. Per LinkedIn/portfolio italiano.
2. **`pra-demo-en.mp4`** — stessa traccia video, voice-over **rifatto in inglese** (oppure rimosso e rimpiazzato da text-on-screen). Per GitHub README + audience internazionale.

---

## 5. Pubblicazione

| Dove | File | Note |
|---|---|---|
| GitHub README | `pra-demo-en.mp4` | Carica il video direttamente in un Issue/Discussion del repo (GitHub host fino a 100MB), copia l'URL nel README sostituendo il placeholder GIF. |
| Portfolio personale | `pra-demo-it.mp4` (default) + `-en.mp4` | Embed via `<video>` HTML nel sito. |
| LinkedIn post | versione 60s tagliata | LinkedIn premia video < 1min con auto-play. Tagliale beat 5 (DB) e parte del 3. |
| YouTube | entrambe | Crea una playlist "PR Review Agent — Portfolio". Privato finché non lo annunci. |

---

## 6. Cosa NON fare

- ❌ Niente "talking head" della webcam — distrae da ciò che mostri sullo schermo. Solo voice-over.
- ❌ Niente musica di sottofondo. La code review è seria, lascia spazio alle parole.
- ❌ Non mostrare l'API key Anthropic o il `.env` — controlla che nessun terminale faccia leak. Apri `.env` solo se è il `.env.example`.
- ❌ Non lasciare timestamp tipo `2026-05-06T13:21:30Z` sullo schermo — rende il video "datato". Se necessario, blur in editing.
- ❌ Non spiegare il codice riga per riga. Non è un tutorial, è una demo.
- ❌ Non scusarti per imperfezioni live ("scusate la pausa", "non funziona…"). Rifai il take.

---

## 7. Pre-flight checklist (subito prima di REC)

```
[ ] Server uvicorn UP, log puliti
[ ] ngrok UP con dominio statico
[ ] PR #2 aperta su GitHub, già refreshed (mostra stato pre-commit)
[ ] LangSmith UI loggata, project pr-review-agent selezionato
[ ] DBeaver connesso, query salvata pronta
[ ] Terminal 2: comando git commit empty + push pre-incollato in clipboard
[ ] OBS: scene "Demo" selezionata, audio mic a -3dB di picco verificato
[ ] Notifiche Windows: silenziate (Focus Assist ON)
[ ] Notifiche Slack/Discord/Mail: chiuse
[ ] Tema VS Code: dark high-contrast (es. "GitHub Dark Default")
[ ] Browser: ad-blocker ON (no popup), modalità in-private per evitare suggestioni
[ ] Cuffie: indossate per evitare echo dal mic
```

Se uno di questi è "no", risolvilo prima di premere REC. Una settima registrazione non perfetta dimostra che ti tieni in piedi; settima registrazione fatta male dimostra che non ti sei preparato.

Buona registrazione 🎬
