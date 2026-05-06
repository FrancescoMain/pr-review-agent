# W2 · Task 4 — Nodo Reviewer + skip-route

## Cosa è stato consegnato

Il quarto nodo del grafo, quello che produce il **vero giudizio** sul codice, e il primo edge condizionale del grafo.

- **`ReviewResult` Pydantic** con `overall_comment` (markdown), `inline_comments` (lista di `InlineComment(path/line/body/severity)`), `approval` (`approve` / `comment` / `request_changes`). `Severity` StrEnum: `nit / suggestion / issue / blocker`.
- **`make_reviewer_node(github_client, chain_factory, max_inline_comments=20)`**:
  1. Sceglie il modello via `chain_factory(triage.risk_level)` → in produzione **Opus 4.7** se `risk == high`, altrimenti **Sonnet 4.6**.
  2. Fetcha il diff con `github_client.get_pr_diff(...)` (1 chiamata HTTP, no tool loop).
  3. Costruisce il prompt unendo diff + `gathered_context` + linked issues + triage.
  4. Invoca la chain in structured output → `ReviewResult`.
  5. Tronca le inline a 20 con marker "_(N additional inline comment(s) suppressed…)_" appeso a `overall_comment`.
- **`make_default_review_chain_factory(anthropic_api_key, opus_model, sonnet_model)`** è la factory di produzione che il `runner.py` istanzia. La factory è una funzione: i test ne passano una che restituisce un `RunnableLambda` con un `ReviewResult` pre-baked → niente Anthropic in pytest.
- **Edge condizionale post-triage** in `build_graph`: se `triage.should_skip == True` il grafo va dritto al Publisher; altrimenti percorre `context_gatherer → reviewer → publisher`. Niente token sprecati su PR triviali.
- **Publisher in transizione**: ora formatta il `ReviewResult` come issue comment con header `### Review — <approval>`, `overall_comment`, e bullet list inline ("`<path>:<line>` — body" con glyph per severity). Quando solo `triage.should_skip == True`, posta "Skipped review — triage classified this PR as **<type>** (<risk>)…". Fallback W1 hello-world resta per backward-compat dei test. La pubblicazione di **inline comment veri** (via `POST /pulls/{n}/reviews`) è W2 Task 5.
- **Runner cresce di una linea**: builda `make_default_review_chain_factory` e lo passa a `make_reviewer_node`, lo monta nel `build_graph(reviewer=...)`.
- **`AgentState` cresce di un campo**: `review: NotRequired[ReviewResult | None]`.

**Cosa NON è stato fatto:**

- Inline comments **veri** su GitHub via Reviews API — W2 Task 5.
- Critic node — W3.
- Cost tracking + correlation ID — W2 Task 6 / Task 7.
- Skipping del Gatherer in modo "soft" (es. quando il diff è banalissimo ma triage non lo segna skip) — non in scope; il guardrail è il `max_tool_calls=15` del Gatherer.

## Setup dell'ambiente di test

Nessuna dipendenza nuova. I test del Reviewer sono completamente offline (nessun Anthropic, nessun `respx`: il `chain_factory` viene mockato).

## Scenari da testare manualmente

### 1 — Pytest verde

```bash
uv run pytest -q
```

Atteso: **95 passed** (83 dopo W2 Task 3 + 8 nuovi `test_node_reviewer.py` + 4 nuovi in `test_node_publisher.py`; `test_graph.py` cambia a 2 test invece di 1, copertura più ampia con parità di numero ma scenari diversi).

I test nuovi in dettaglio:

- **`test_node_reviewer.py` (8)**:
  - `test_reviewer_returns_structured_review` — happy path; `factory` viene chiamato con `RiskLevel.medium`, `get_pr_diff` con i tre id corretti.
  - `test_reviewer_passes_diff_and_context_to_chain` — il dict di input alla chain contiene diff, gathered, linked_issues, triage fields.
  - `test_reviewer_routes_high_risk_to_opus_branch` — `risk=high` → `factory` riceve `RiskLevel.high`. Routing Opus/Sonnet vive nella factory di produzione, non nel nodo.
  - `test_reviewer_passes_none_when_triage_missing` — `triage=None` → `factory(None)`.
  - `test_reviewer_caps_inline_comments` — 30 in input → 20 in output, marker nel `overall_comment`.
  - `test_reviewer_does_not_touch_overall_when_under_cap` — sotto cap, `overall_comment` invariato.
  - `test_reviewer_propagates_github_api_error` — `GitHubAPIError` da `get_pr_diff` propaga.
  - `test_reviewer_propagates_chain_failure` — `RuntimeError` dalla chain (modello che ritorna JSON malformato dopo retry) propaga.
- **`test_node_publisher.py` (+4)**: review presente → comment formattato; review + skip → review wins; skip senza review → "Skipped review"; nessun cambio sul fallback W1.
- **`test_graph.py` (riscritto, 2 test)**:
  - Route normale: triage → gatherer → reviewer → publisher; il `final_comment` contiene il body del review e `path:line`.
  - Route skip: gatherer e reviewer **non vengono visitati** (gli passiamo un `_abort_node` che fail-fast); publisher posta "Skipped review".

### 2 — Bruno collection verde (sanity check)

Niente endpoint nuovi:

```bash
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  uv run uvicorn pr_review_agent.main:app --port 8001 &
until curl -sf http://localhost:8001/health > /dev/null; do sleep 0.2; done
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  bash -lc 'cd bruno && npx --yes @usebruno/cli run --env local'
kill %1
```

Atteso: stesso risultato di W2 Task 3.

### 3 — Reviewer isolato via REPL (nessun Anthropic, nessun GitHub)

```bash
uv run python - <<'PY'
import asyncio
from langchain_core.runnables import RunnableLambda
from pr_review_agent.agent.models import (
    ApprovalLevel, ChangeType, GatheredContext, InlineComment,
    LinkedIssue, ReviewResult, RiskLevel, Severity, TriageDecision,
)
from pr_review_agent.agent.nodes.reviewer import make_reviewer_node

class StubClient:
    async def get_pr_diff(self, *, installation_id, repo, pr_number):
        return "diff --git a/x b/x\n@@\n+new line\n"

review = ReviewResult(
    overall_comment="LGTM with one nit.",
    inline_comments=[InlineComment(path="x", line=5, body="rename foo", severity=Severity.nit)],
    approval=ApprovalLevel.comment,
)

state = {
    "repo":"x/y","pr_number":1,"installation_id":99,
    "head_ref":"feat/x","head_sha":"0"*40,"pr_title":"t","pr_body":"",
    "triage":TriageDecision(change_type=ChangeType.feature, risk_level=RiskLevel.high),
    "gathered_context":GatheredContext(summary="adds X", relevant_files=["x"], linked_issues=[]),
}

received_risks = []
def factory(risk):
    received_risks.append(risk)
    return RunnableLambda(lambda _i: review)

node = make_reviewer_node(github_client=StubClient(), chain_factory=factory)
result = asyncio.run(node(state))
print("review:", result["review"])
print("factory called with:", received_risks)
PY
```

Atteso: stampa il `ReviewResult` e `factory called with: [<RiskLevel.high: 'high'>]` (il routing ha funzionato — in produzione questo lato sceglie Opus).

### 4 — End-to-end con LLM reale

Solo se vuoi vedere il modello scrivere review veri. Apri una PR sul playground; tail dei log:

```bash
uv run uvicorn pr_review_agent.main:app --reload
```

Cosa cercare:

- Il commento posato dal Publisher contiene `### Review — comment` (o `request_changes`/`approve`) seguito dal markdown e dalla bullet list inline. Se la PR è triviale e il triage la marca skip, vedi invece `Skipped review`.
- LangSmith trace: tre run annidati — triage, context_gatherer (sub-graph), reviewer.
- Il modello scelto è Opus se `risk_level=high`, altrimenti Sonnet 4.6. Verifica nei metadata del trace.
- Cost: aspettati ~$0.05-0.20 per PR su Sonnet, ~$0.30-1.00 su Opus high-risk. **Cost cap arriva in W3.**

### 5 — Verifica errore: structured output che fallisce

Per simulare un modello che restituisce JSON malformato, in `runner.py` cambia temporaneamente la `chain_factory` con una che ritorna un `RunnableLambda` che raise `RuntimeError`. Atteso: il run fallisce, `_run_agent_safely` logga `agent run failed`, il webhook ritorna comunque 202.

### 6 — Verifica skip route

Apri una PR con titolo `chore: typo, no review needed` e body con la stessa frase. Il triage (Haiku 4.5) dovrebbe alzare `should_skip=True`. Atteso: il commento posato è `Skipped review — triage classified this PR as **chore** (low) and marked it as not needing review.` e nel trace LangSmith vedi **solo** triage + publisher (gatherer e reviewer non sono mai chiamati).

## Cosa cercare nei log

- `agent run failed` con stacktrace → fallimento (clone, GitHub API, chain Anthropic).
- LangSmith ha tutto il dettaglio: input prompt, output strutturato, model pricing.
- Niente log dedicati al Reviewer in questa task — correlation ID arriva W2 Task 6.

## Limiti dichiarati

- **`overall_comment` può essere lungo arbitrariamente.** Cap solo sulle inline. Se il modello scrive un romanzo, lo pubblichiamo. Se diventa un problema, mettiamo un cap a 4000 char in W3.
- **Inline comment con `line` errato** (modello sbaglia il number sul diff) → in Task 4 finisce nel bullet list e il developer vede `path:line` puntare alla riga sbagliata. In Task 5 useremo l'API di GitHub Reviews che valida `line` contro il diff: se è invalida l'API ritorna 422 e dovremo decidere come fallback (skip-comment vs commento generale).
- **Routing Opus/Sonnet hardcoded a `risk == high`**. Per ora è semplice. Se l'eval di W4 mostra che Opus migliora anche su `medium`, lo estendiamo.
- **Cost cap = 0 oggi.** Una PR mostruosa con review_depth=deep + risk=high può costare. È il rischio noto, mitigato in W3.
- **Edge condizionale solo dopo triage.** Non c'è skip dopo il Gatherer (es. "il diff è vuoto, lascia perdere"). Non lo aggiungiamo finché l'eval non ce lo chiede.

## Riferimenti file

- `src/pr_review_agent/agent/models.py` — `Severity`, `ApprovalLevel`, `InlineComment`, `ReviewResult`.
- `src/pr_review_agent/agent/state.py` — campo nuovo `review`.
- `src/pr_review_agent/agent/nodes/reviewer.py` — nodo + `make_default_review_chain_factory`.
- `src/pr_review_agent/agent/prompts/reviewer.md` — system prompt.
- `src/pr_review_agent/agent/graph.py` — edge condizionale `_route_after_triage` + nodo reviewer.
- `src/pr_review_agent/agent/nodes/publisher.py` — formatta review / skip / hello-world.
- `src/pr_review_agent/agent/runner.py` — istanzia Reviewer.
- `tests/unit/test_node_reviewer.py` — 8 test.
- `tests/unit/test_node_publisher.py` — 4 nuovi test.
- `tests/unit/test_graph.py` — 2 test (riscritto).
