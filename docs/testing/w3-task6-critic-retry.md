# W3 · Task 6 — Nodo Critic + retry edge

## Cosa è stato consegnato

Il **quinto nodo** del grafo: dopo che il Reviewer produce la draft, il Critic fa una passata di QA. Combina due livelli (validazione deterministica delle line numbers + giudizio LLM su tone/severity/scope) e decide se accettare o **rimandare al Reviewer per un'ultima riscrittura**. Massimo 1 retry — sopra, accetta ciò che c'è.

- **`VerdictKind`** StrEnum (`accept` / `revise`).
- **`CriticVerdict`** Pydantic con `verdict`, `concerns: list[str]`, `should_drop_inline: list[InlineComment]`, `revised_overall_comment: str | None` (hint, non vincolante).
- **`AgentState` cresce di** `critic_verdict: CriticVerdict | None` e `retry_count: int`.
- **`agent/nodes/critic.py`** con `make_critic_node(chain_factory)`:
  - **Layer 1 — deterministic**: `parse_post_lines(raw_diff)` → ogni inline con `(path, line)` non nel diff finisce in `should_drop_inline`, anche se l'LLM non l'ha flaggato.
  - **Layer 2 — LLM** (Haiku 4.5, ~$0.001 per chiamata): valuta tone, severity inflation, out-of-scope, contraddizioni, hallucinations non legate alle line numbers.
  - **Merge**: union dei due drop list, dedup per `(path, line, body)`.
  - **Hard rule**: se i drop superano il 50% degli inline, forza `verdict=revise` indipendentemente dall'LLM.
  - Edge case difensivi: `review=None` → accept; `raw_diff` mancante → ogni inline è hallucinato → forced revise.
- **Graph topology**:
  ```
  triage ─→ (skip?) ─→ publisher
       ↓ no
  gatherer ─→ reviewer ─→ critic
                   ↑          ↓
                   └─ revise ─┤  (≤ 1 retry)
                              ↓ accept / forced
                          publisher
  ```
  Edge condizionale `_route_after_critic`: `revise` AND `retry_count < MAX_REVIEW_RETRIES (=1)` → reviewer; altrimenti → publisher. La cap di 1 retry è hardcoded nel modulo `graph` (configurabile in W4 se serve).
- **Reviewer in retry**: il prompt include un blocco "Critic feedback" con i `concerns`, gli inline da droppare, e l'opzionale `revised_overall_comment` come hint. Quando re-invocato dopo Critic, incrementa `retry_count` di 1 (così il routing termina la loop).
- **Publisher applica `should_drop_inline`** silenziosamente prima della pubblicazione: matcha per `(path, line, body)`, rimuove le inline corrispondenti dal `ReviewResult` (via `model_copy`). Non tocca `approval` (decisione del Reviewer). Niente nota sul commento — la trasparenza è in LangSmith trace.
- **Runner** wirea il Critic con `make_default_critic_chain_factory(anthropic_api_key)` (Haiku) e lo passa a `build_graph(critic=...)`.

**Cosa NON è stato fatto:**

- Cap retry > 1 configurabile via Settings — hardcoded a 1. Configurabile se in W3-Task7 (eval) vediamo che 2 migliorerebbero la qualità.
- Critic capability di alterare `approval` — il Reviewer mantiene autorità sul verdetto finale.
- Nota nel commento finale ("Critic dropped X inline") — se vorrai trasparenza utente, lo aggiungiamo come opt-in.
- Aggiornamento di `revised_overall_comment` come override automatico — è solo un hint per il Reviewer.

## Setup dell'ambiente di test

Test pytest **completamente offline**: chain factory injection (RunnableLambda con CriticVerdict pre-baked). Niente Anthropic.

## Scenari da testare manualmente

### 1 — Pytest verde

```bash
uv run pytest -q
```

Atteso: **204 passed** (193 dopo W3-Task5 + 7 nuovi `test_node_critic.py` + 2 nuovi `test_graph.py` (loop + retry cap) + 2 nuovi `test_node_reviewer.py` (critic feedback + retry_count) + 2 nuovi `test_node_publisher.py` (drop inline)).

I nuovi test:

- **`test_node_critic.py` (7)**:
  - `accepts_clean_review` — happy path, niente drops, verdict=accept.
  - `drops_anchors_not_in_diff_even_when_llm_accepts` — deterministic layer cattura hallucinated line; >50% rule → revise.
  - `merges_llm_drops_with_deterministic_drops` — union deduplicata.
  - `propagates_llm_verdict_when_no_drops` — LLM revise puro passa con concerns + revised_overall.
  - `surfaces_concern_when_only_silently_dropping` — accept ma 1/3 drops → concern aggiunto.
  - `accepts_when_review_is_missing` — defensive: review=None → accept, chain non invocata.
  - `handles_missing_diff_gracefully` — raw_diff mancante → forced revise.
- **`test_graph.py` (4 totali, 2 nuovi)**:
  - `loops_when_critic_revises_then_accepts` — flipping critic, reviewer chiamato 2 volte, retry_count=1, publisher fired.
  - `force_publishes_after_retry_cap` — critic sempre revise, dopo retry=1 il routing forza il publisher.
- **`test_node_reviewer.py` (+2)**:
  - `includes_critic_feedback_on_retry` — il dict di input ha `critic_feedback` con concerns + revised_overall + previous draft. retry_count incrementato.
  - `does_not_increment_retry_count_on_first_pass` — primo pass, niente retry_count nell'update.
- **`test_node_publisher.py` (+2)**:
  - `drops_inline_flagged_by_critic` — la inline marcata in `should_drop_inline` non appare nei comments né nel body.
  - `keeps_review_unchanged_when_critic_drops_nothing` — verdict accept senza drops → passthrough.

### 2 — Bruno collection verde (sanity check)

Niente endpoint nuovi.

### 3 — Smoke E2E con LLM reale

Server up come al solito (DATABASE_URL + QDRANT_URL + GitHub App). Apri/sincronizza una PR sul playground.

Atteso nei log:
- run di `triage` → `context_gatherer` → `reviewer` → `critic` → (eventualmente loop reviewer + critic) → `publisher`.
- LangSmith trace mostra il sub-grafo del critic con il suo prompt + verdict strutturato.
- Se la PR è "buona" e il Reviewer fa un buon lavoro, il critic accetta al primo giro (no loop).
- Se il Reviewer hallucina (es. commenta riga 99 di un file che ha solo 10 righe), il critic deterministic drops la inline — e se sono >50% delle inline, forza revise → reviewer rifa con feedback.

Verifica record DB:

```bash
docker compose exec postgres psql -U postgres -d pr_review_agent -c \
"SELECT id, correlation_id, status, tool_calls_used, tokens_input, tokens_output, cost_usd FROM agent_runs ORDER BY id DESC LIMIT 3;"
```

I run con retry hanno `tokens_output` significativamente più alti (Reviewer chiamato 2 volte). Niente colonna esplicita `retry_count` nel DB per ora — visibile solo via LangSmith trace.

### 4 — Verifica drop inline

Modifica temporaneamente il prompt del Reviewer (o accetta che il modello lo faccia) per produrre un inline su una riga inesistente. Atteso:
- `agent_runs` mostra un run normale `status='success'`.
- Il commento sulla PR NON contiene quel inline ma sì un nota nei `concerns` del critic in LangSmith.
- Se la PR "ha solo 1 inline e quello era hallucinato" → critic forza `revise` → reviewer riprova.

### 5 — Verifica retry cap

Force il critic a `revise` sempre (es. modifica `make_default_critic_chain_factory` per ritornare sempre revise). Atteso: 2 reviewer runs (initial + 1 retry), poi publisher. `retry_count=1` in stato finale. Cost approssimativamente raddoppia rispetto a un run normale.

## Cosa cercare nei log

- Niente log dedicati `critic.*` per ora (vive solo in LangSmith). Future-task: aggiungere `critic.verdict verdict=revise concerns=[...]`.
- Il run dell'agente in `agent_runs` riflette i token totali, non per-nodo. Per debug per-nodo: LangSmith.

## Limiti dichiarati

- **Cap retry hardcoded a 1**. Modificarlo richiede edit di `MAX_REVIEW_RETRIES` in `graph.py`. In W4 lo facciamo configurabile.
- **Nessuna persistenza del `critic_verdict`** (concerns, drops) nel DB. Vive in LangSmith trace e nel `final_state` in-memory. Se in dashboard di W4 servirà filtrare per "run con drops", aggiungiamo colonne dedicate.
- **Layer LLM (Haiku) può sbagliare anche lui** — è perché il deterministic layer fa da safety net per le line numbers, e il retry cap evita loop infinite.
- **Publisher silenzioso sui drops** — il developer non vede "Critic ha rimosso 3 inline" nel commento. Trasparenza solo in LangSmith. Se in eval di Task 7 vediamo che diventa un problema, lo rendiamo visibile.
- **Reviewer in retry rilegge il diff via `get_pr_diff` un'altra volta** — extra HTTP call. Per ora va bene; in W4 valutiamo cache nel `state.raw_diff`.

## Riferimenti file

- `src/pr_review_agent/agent/models.py` — `VerdictKind`, `CriticVerdict`.
- `src/pr_review_agent/agent/state.py` — `critic_verdict`, `retry_count`.
- `src/pr_review_agent/agent/prompts/critic.md` — system prompt.
- `src/pr_review_agent/agent/nodes/critic.py` — node + `make_default_critic_chain_factory`.
- `src/pr_review_agent/agent/graph.py` — `_route_after_critic` + `MAX_REVIEW_RETRIES`.
- `src/pr_review_agent/agent/runner.py` — wiring del critic.
- `src/pr_review_agent/agent/nodes/reviewer.py` — `_format_critic_feedback` + retry_count + prompt template "Critic feedback" block.
- `src/pr_review_agent/agent/prompts/reviewer.md` — system prompt aggiornato.
- `src/pr_review_agent/agent/nodes/publisher.py` — `_apply_critic_drops`.
- `tests/unit/test_node_critic.py` — 7 nuovi test.
- `tests/unit/test_graph.py` — riscritto, 4 test (2 nuovi sul loop).
- `tests/unit/test_node_reviewer.py` — 2 nuovi test.
- `tests/unit/test_node_publisher.py` — 2 nuovi test.
