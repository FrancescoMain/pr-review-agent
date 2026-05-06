# W3 · Task 1 — Cost cap per PR

## Cosa è stato consegnato

Il primo guardrail "non spendere troppo" del progetto. Quando il costo accumulato di un run supera la soglia configurata, l'agente abortisce pulitamente: posta un commento sulla PR con il dettaglio numerico, scrive lo stato `aborted_cost` su Postgres, e termina il run senza propagare un'eccezione al webhook.

- **`Settings.cost_cap_per_pr_usd: float`** già esisteva (default `0.50`); ora è cablato. Il `lifespan` lo passa al runner come `Decimal(str(settings.cost_cap_per_pr_usd))` per evitare il drift binario di `Decimal(0.5)`.
- **Nuova eccezione `CostCapExceeded`** in `src/pr_review_agent/agent/exceptions.py`. Porta `current_cost: Decimal` e `cap: Decimal` come attributi → il messaggio di abort sulla PR può citare i numeri esatti.
- **`CostTrackingCallback(cost_cap_usd: Decimal | None = None)`**. In `on_llm_end`, dopo aver aggiornato i totali, calcola il costo corrente via `cost_table.compute_cost_usd` per modello e raise se supera. `None` = nessun cap (utile per dev / test che non vogliono il guardrail).
- **Trigger live**: il check si attiva alla prima LLM call che fa superare la soglia. Significa che il loop del Gatherer (Sonnet) viene fermato sul primo step "fuori budget", e il Reviewer (Sonnet/Opus, single shot) non parte se Gatherer ha già esaurito il budget.
- **`make_default_runner(..., cost_cap_usd: Decimal | None = None)`**. Il try/except attorno a `graph.ainvoke` cattura **solo** `CostCapExceeded` come abort path; tutto il resto (GitHub/Anthropic 5xx, signature, ecc.) continua a essere `record_run_failed` come oggi.
- **`_abort_for_cost_cap` helper** posta `🛑 Review aborted: cost cap reached at $X.XXXXXX (cap: $Y.YYYYYY). Re-run after raising COST_CAP_PER_PR_USD or splitting the PR.` come issue comment, logga `cost_cap.exceeded` con structlog, e scrive `record_run_finished(status='aborted_cost', ...)` con i totali parziali (token già consumati + per_model JSONB).
- **DB**: nessuna migrazione. `agent_runs.status` è `TEXT`, accetta il nuovo valore `'aborted_cost'` direttamente. Filtri/dashboard di W4 distingueranno success / skipped / aborted_cost / failure.

**Cosa NON è stato fatto:**

- Cap separato per nodo (es. cap_gatherer vs cap_reviewer). Per ora il cap globale di run basta. Lo aggiungiamo se l'eval di W3-Task6 mostra che il Gatherer mangia tutto il budget e il Reviewer abort-a senza poter scrivere niente.
- Stima preventiva del costo in base al `risk_level` triage. È overengineering finché non vediamo PR concrete che esauriscono il cap.
- Pre-fetching del cost cap da una tabella DB (per cap dinamici per repo). Ora è solo `Settings`. Lo affronteremo in deploy multi-repo.

## Setup dell'ambiente di test

Nessuna dipendenza nuova. I test sono offline: il callback raise direttamente, il runner abort path è testato in isolamento via `_abort_for_cost_cap` (non serve istanziare la pipeline LangChain).

## Scenari da testare manualmente

### 1 — Pytest verde

```bash
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest -q
```

Atteso: **144 passed** (137 dopo W2 + 3 nuovi `test_cost_callback.py` + 4 nuovi `test_runner_cost_cap.py`).

I nuovi test:

- **`test_cost_callback.py` (+3)**:
  - `test_callback_without_cap_never_raises` — 5 invocazioni Opus a $90 ciascuna (totale $450) e nessuna eccezione (cap=`None`).
  - `test_callback_raises_when_first_call_exceeds_cap` — cap $0.001 e Sonnet 100 in / 50 out → $0.00105 al primo `on_llm_end` → `CostCapExceeded` con i campi `cap` e `current_cost` corretti.
  - `test_callback_raises_only_when_cap_is_actually_crossed` — primo step sotto cap (no raise), secondo step sopra (raise). Verifica che non raise prima del momento giusto.
- **`test_runner_cost_cap.py` (4)**:
  - `test_abort_posts_comment_and_records_aborted_cost` — la chiamata a `_abort_for_cost_cap` posta il comment con i numeri ($current, $cap), e l'`UPDATE agent_runs` ha `status='aborted_cost'`, triage propagato, `tool_calls_used`/token/per_model dai totali parziali.
  - `test_abort_does_not_raise_if_comment_post_fails` — se `post_pr_comment` raise (token revocato), il DB write è comunque tentato e non propaga.
  - `test_abort_tolerates_missing_db_pool` — `db_pool=None` / `run_id=None`: solo il comment va, niente errore.
  - `test_abort_message_contains_actionable_hint` — il body contiene `COST_CAP_PER_PR_USD` per dare al developer la leva su cui agire.

### 2 — Bruno collection verde

Niente endpoint nuovi. Stesso comando di W2 Task 7:

```bash
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  uv run uvicorn pr_review_agent.main:app --port 8001 &
until curl -sf http://localhost:8001/health > /dev/null; do sleep 0.2; done
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  bash -lc 'cd bruno && npx --yes @usebruno/cli run --env local'
kill %1
```

### 3 — Smoke E2E con cap basso

Forza un abort impostando un cap ridicolmente basso (es. `$0.001`) e triggera una PR sul playground:

```bash
DATABASE_URL=postgresql://postgres:postgres@localhost:5433/pr_review_agent \
COST_CAP_PER_PR_USD=0.001 \
  uv run uvicorn pr_review_agent.main:app --port 8001
```

Poi triggera (commit empty sul branch della PR):

```bash
cd /tmp/<playground-clone> && \
  git -c user.name=Francesco -c user.email=cesaranofrancescomain@gmail.com \
      commit --allow-empty -m "trigger cost cap test" && \
  git push
```

Atteso:

- Sulla PR appare un singolo issue comment `🛑 Review aborted: cost cap reached at $0.001XXXXX (cap: $0.001000). Re-run after raising COST_CAP_PER_PR_USD or splitting the PR.`.
- Nei log: `cost_cap.exceeded repo=… pr_number=… current_cost=… cap=…` come warning.
- In DB: nuova riga `status='aborted_cost'` con i token parziali del solo triage.

```bash
docker compose exec postgres psql -U postgres -d pr_review_agent -c \
"SELECT id, correlation_id, status, tool_calls_used, tokens_input, tokens_output, cost_usd FROM agent_runs WHERE status='aborted_cost' ORDER BY id DESC LIMIT 3;"
```

### 4 — Smoke con cap normale (default $0.50)

Senza override (`unset COST_CAP_PER_PR_USD`), triggera la stessa PR. Atteso: run completo come Task 7, **niente** abort. Il `cost_usd` finale resta sotto $0.50 per PR di dimensione normale.

### 5 — Cap esattamente raggiunto

Prova `COST_CAP_PER_PR_USD=0.001050` e fai partire un run. Il primo `on_llm_end` (triage) dovrebbe portarci esattamente al limite. La logica usa `>` (strict), non `>=`: arrivare al cap senza superarlo NON abort. Solo il secondo evento (gatherer) abort. Utile per debugging.

## Cosa cercare nei log

- `cost_cap.exceeded` (warning) → l'abort è scattato. Sempre accompagnato da `current_cost` e `cap`.
- `could not post cost-cap abort comment` (exception) → la post del comment è fallita ma il DB è comunque aggiornato. Indagare token GitHub.
- `could not write aborted_cost row to agent_runs` (exception) → DB write fallita; il record di abort manca. Indagare connessione Postgres.
- LangSmith trace è troncato: vedi gli eventi LLM fino al momento del raise. Utile per capire chi ha "rotto" il budget.

## Limiti dichiarati

- **Cap globale, non per nodo.** Se un Gatherer pessimo brucia tutto il budget, il Reviewer non parte. Nel commento di abort lo dichiariamo (`Re-run after raising COST_CAP_PER_PR_USD or splitting the PR`).
- **Trigger live, non preventivo.** Spendiamo *almeno* la chiamata che fa superare il cap, non possiamo fermarci prima. Per Sonnet/Haiku è centesimi; per Opus su PR grandi può essere fino a $0.30 oltre il cap. È accettato.
- **Conversione `float → Decimal` via `str()`.** Se Pydantic esponesse `cost_cap_per_pr_usd` come `Decimal` direttamente, salteremmo questo passaggio. Per ora resta `float` per compatibilità con `.env` e con `cost_cap_per_pr_usd: 1.25` come stringa nei test.
- **Nessun retry-with-larger-cap automatico.** È a carico del developer rilanciare con un cap più alto. Coerente con la semantica "guardrail di sicurezza".
- **`post_pr_comment` per l'abort, NON Reviews API.** È un commento di servizio, non una review: niente inline comments, niente `event=COMMENT`. Coerente con skip / hello-world / fallback.

## Riferimenti file

- `src/pr_review_agent/agent/exceptions.py` — `CostCapExceeded`.
- `src/pr_review_agent/agent/cost_callback.py` — `cost_cap_usd` parameter + `on_llm_end` check + `_current_total_cost`.
- `src/pr_review_agent/agent/runner.py` — `cost_cap_usd` parameter + try/except + `_abort_for_cost_cap`.
- `src/pr_review_agent/main.py` — passa `Decimal(str(settings.cost_cap_per_pr_usd))` al runner.
- `tests/unit/test_cost_callback.py` — 3 nuovi test (totale 9).
- `tests/unit/test_runner_cost_cap.py` — 4 nuovi test.
