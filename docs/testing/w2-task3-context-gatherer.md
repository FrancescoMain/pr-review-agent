# W2 · Task 3 — Nodo Context Gatherer

## Cosa è stato consegnato

Il primo nodo "intelligente" del grafo: il **Context Gatherer** decide quali tool chiamare per capire cosa la PR sta cambiando, accumula il risultato, e produce un `GatheredContext` strutturato che il Reviewer userà come input.

- **Sub-grafo LangGraph** dietro `make_context_gatherer_node(model, repo_tools, max_tool_calls=15)`. Due nodi interni (`model_step` + `tool_step`) e un edge condizionale `should_continue`. Da fuori il Gatherer si presenta come un singolo nodo `AgentState → partial AgentState`.
- **Tool synthetic `final_answer(summary, relevant_files, notes)`** che il modello chiama quando ha finito di raccogliere contesto. I parametri vengono parsati in un `GatheredContext` Pydantic; se la validazione fallisce, l'errore torna al modello come `ToolMessage` e il modello può ritentare.
- **Discriminazione errori dei tool:**
  - `ToolPathError`, `ValueError`, `Exception generico` → tornano al modello come `ToolMessage("error: ...")`. Il modello impara dall'errore e prova un'altra strada.
  - `GitHubAPIError`, `GitHubAuthError`, `RepoCloneError` → **abort del run**. Sono fallimenti di infrastruttura: non ha senso che il modello ritenti.
- **Cap a 15 tool call** (default, configurabile via parametro). Quando il contatore supera, il loop termina forzatamente con `gathered_context=None` e il Reviewer riceverà un contesto incompleto. È un guardrail di costo.
- **Modello Sonnet 4.6** (`claude-sonnet-4-6`) wired in `runner.py`, timeout 60s, max_retries 2.
- **`PRContext` cresce con `head_ref` + `head_sha`**, `AgentState` cresce con `gathered_context`, `gatherer_messages`, `tool_calls_used`. Il webhook handler popola `head_ref`/`head_sha` da `pr_event.pull_request.head`.
- **Runner per-run**: il `RepoCheckout` viene aperto per la durata del run, i tool sono costruiti con il `checkout.root` del momento, il grafo è ricomposto per ogni PR. Cleanup garantito su success/failure.

**Cosa NON è stato fatto:**

- Reviewer node (W2 Task 4) — il `gathered_context` resta in stato ma nessuno lo legge ancora; il Publisher continua a postare il "hello-world" di W1 con la triage decision.
- Inline comments (W2 Task 5).
- Cost tracking + correlation ID (W2 Task 6 / Task 7).
- Skipping del Gatherer quando `triage.should_skip=True` — lo aggiungiamo in Task 4 quando si introduce l'edge condizionale post-triage.

## Setup dell'ambiente di test

Nessuna dipendenza nuova. Per i test pytest serve solo `git` (già richiesto da Task 2). Per i test end-to-end con LLM reale serve `ANTHROPIC_API_KEY` come prima.

## Scenari da testare manualmente

### 1 — Pytest verde

```bash
uv run pytest -q
```

Atteso: **83 passed** (76 dopo W2 Task 2 + 7 nuovi `test_node_context_gatherer.py`; il vecchio `test_graph.py` cresce di 0 test ma cambia il setup per includere il nuovo nodo).

I 7 nuovi test, in ordine:

- `test_gatherer_produces_gathered_context_via_final_answer` — happy path con 2 tool call + final_answer; `GatheredContext` torna nello state, `tool_calls_used == 3`.
- `test_gatherer_recovers_from_tool_path_error` — primo tentativo `read_file('missing.py')` fallisce, secondo va a buon fine, final_answer chiude. Il messaggio d'errore appare nel trace.
- `test_gatherer_force_ends_at_max_tool_calls` — modello che non chiama mai final_answer; il loop si interrompe a `max_tool_calls=3` con `gathered_context=None`.
- `test_gatherer_ends_when_model_emits_no_tool_calls` — dopo 1 tool call il modello produce un AIMessage senza tool_calls; il loop esce immediatamente.
- `test_gatherer_propagates_github_api_error` — `get_pr_diff` solleva `GitHubAPIError`; il nodo rilancia, niente tool message di "error".
- `test_gatherer_rejects_invalid_final_answer_and_keeps_looping` — primo final_answer ha tipi sbagliati (summary=int), feedback torna al modello, secondo tentativo va.
- `test_gatherer_handles_unknown_tool_gracefully` — modello alluinizza `definitely_not_a_tool`, il nodo ritorna "unknown tool" come ToolMessage e il modello si recupera.

### 2 — Bruno collection verde (sanity check)

Niente endpoint nuovi. Verifichiamo che la signature di `make_default_runner` non abbia rotto il lifespan:

```bash
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  uv run uvicorn pr_review_agent.main:app --port 8001 &
until curl -sf http://localhost:8001/health > /dev/null; do sleep 0.2; done
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  bash -lc 'cd bruno && npx --yes @usebruno/cli run --env local'
kill %1
```

Atteso: stesso risultato di W2 Task 2.

### 3 — Esecuzione del Gatherer isolata via REPL (no GitHub, no checkout, no Anthropic)

Stesso pattern del Task 2 §3 ma con il sub-grafo del Gatherer. Si inietta uno `_ScriptedChatModel` che torna AIMessage scritti a mano.

```bash
uv run python - <<'PY'
import asyncio
from typing import Any, Sequence
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import tool
from pr_review_agent.agent.models import (
    ChangeType, RiskLevel, TriageDecision,
)
from pr_review_agent.agent.nodes.context_gatherer import make_context_gatherer_node

class Scripted(FakeMessagesListChatModel):
    def bind_tools(self, tools: Sequence[Any], **kw: Any) -> Runnable[Any, Any]: return self

@tool
async def get_pr_diff() -> str:
    """Return the unified diff of the PR."""
    return "diff --git a/x b/x\n+++ b/x\n@@\n-old\n+new\n"

@tool
async def get_linked_issues() -> list[dict[str, Any]]:
    """Return linked issues."""
    return [{"number": 1, "title": "old bug", "state": "open", "body": ""}]

@tool
async def read_file(path: str) -> str:
    """Read a file from the PR head."""
    return f"# fake contents of {path}\n"

@tool
async def list_directory(path: str) -> list[str]:
    """List a directory."""
    return ["main.py", "test_main.py"]

@tool
async def search_code(query: str, file_pattern: str | None = None) -> list[dict[str, Any]]:
    """Search code."""
    return []

scripted = Scripted(responses=[
    AIMessage(content="", tool_calls=[{"name":"get_pr_diff","args":{},"id":"a"}]),
    AIMessage(content="", tool_calls=[{"name":"read_file","args":{"path":"main.py"},"id":"b"}]),
    AIMessage(content="", tool_calls=[{"name":"final_answer","args":{
        "summary":"Trivial typo fix in main.py",
        "relevant_files":["main.py"],
        "notes":"",
    },"id":"c"}]),
])

node = make_context_gatherer_node(
    model=scripted,
    repo_tools=[get_pr_diff, get_linked_issues, read_file, list_directory, search_code],
    system_prompt="You gather context.",
)

state = {
    "repo":"x/y","pr_number":1,"installation_id":99,
    "head_ref":"feat/x","head_sha":"0"*40,
    "pr_title":"fix typo","pr_body":"",
    "triage":TriageDecision(change_type=ChangeType.bugfix, risk_level=RiskLevel.low),
}

result = asyncio.run(node(state))
print("gathered_context:", result["gathered_context"])
print("tool_calls_used:", result["tool_calls_used"])
print("messages count:", len(result["gatherer_messages"]))
PY
```

Cosa aspettarsi:

- `gathered_context` stampato come `summary='Trivial typo fix in main.py' relevant_files=['main.py'] linked_issues=[] notes=''`.
- `tool_calls_used: 3`.
- `messages count` ≥ 5 (system + human + 3 AI + 3 tool message).

### 4 — End-to-end con LLM reale (richiede `ANTHROPIC_API_KEY`)

Solo se vuoi vedere il modello vero scegliere i tool. Non è automatizzato — costa qualche cent per run.

```bash
# .env già configurato con ANTHROPIC_API_KEY + GITHUB_*.
# Apri una PR di test sul playground: il webhook → triage → gatherer → publisher.
# Tail dei log:
uv run uvicorn pr_review_agent.main:app --reload
```

Cosa cercare:

- Il commento posato dal publisher è sempre lo "hello-world", **invariato** rispetto a Task 5 di W1 (il Reviewer arriva in Task 4). Il Gatherer **gira** ma nessuno legge ancora il suo output.
- In LangSmith dovrebbe apparire un trace con il **sub-grafo del Gatherer** annidato sotto il run del grafo principale, con i tool call visibili.
- Se la PR ha un body con `Closes #N`, dovresti vedere `get_linked_issues` chiamato. Diff > 200 KB → `read_file` ritorna troncato (non rilevante per Gatherer in sé, ma utile sapere).
- **Cleanup tmpdir:** dopo che il run finisce, `ls /tmp/pr-review-checkout-*` non deve mostrare nulla.

### 5 — Verifica errore: `RepoCloneError` propagato

Per simulare il fallimento del clone (es. token revocato, repo eliminato), in `runner.py` cambia temporaneamente `head_sha=state["head_sha"]` con `head_sha="0"*40`. Atteso: il run del grafo solleva `RepoCloneError` che il `webhook._run_agent_safely` cattura e logga via structlog con `agent run failed`. Il webhook ritorna comunque 202 (il fallimento è in background). Rimuovi la modifica per tornare verde.

## Cosa cercare nei log

- `agent run failed` con stacktrace → fallimento di infrastruttura (clone, GitHub API, auth).
- Niente log dedicato per il Gatherer in questa task — arriva con il correlation ID (W2 Task 6).
- LangSmith trace è la fonte di verità per "il modello ha chiamato il tool giusto?". Il `system_prompt` del Gatherer è in `prompts/context_gatherer.md`.

## Limiti dichiarati

- **Costruzione del grafo per-run.** Il grafo viene rebuilt a ogni PR (per binding dei tool al checkout). È OK per W2 (latenza nulla), ma se in deploy vediamo un overhead misurabile, valutiamo un design con il Gatherer come "nodo lazy" che costruisce il sub-grafo on-demand.
- **`bind_tools` schema.** Le tool description (docstring) viaggiano dentro i system prompt del modello. Se cambi una docstring, cambi l'API che il modello vede — potenziale regressione di comportamento. In W3 valutiamo se versionarle.
- **`max_tool_calls=15` è hardcoded come default**. Rimane il valore di SPEC. Se in eval vediamo che troncare a 15 è troppo basso per PR grandi, lo alziamo o lo facciamo dipendere da `triage.review_depth`.
- **Niente retry sui 5xx Anthropic.** `ChatAnthropic(max_retries=2)` sta facendo già il retry HTTP. Se il modello restituisce un AIMessage strutturalmente sbagliato, il sub-grafo lo gestisce come tool error — ma se Anthropic risponde 503 dopo 2 retry, il run fallisce.
- **Il body della PR è snapshot al dispatch del runner.** Edit del body durante il run non sono visti dai tool. Voluto: deterministico per il run.

## Riferimenti file

- `src/pr_review_agent/agent/models.py` — `GatheredContext`.
- `src/pr_review_agent/agent/state.py` — campi nuovi (`gathered_context`, `gatherer_messages`, `tool_calls_used`).
- `src/pr_review_agent/agent/nodes/context_gatherer.py` — sub-grafo + factory.
- `src/pr_review_agent/agent/prompts/context_gatherer.md` — system prompt.
- `src/pr_review_agent/agent/graph.py` — ora prende anche `context_gatherer`.
- `src/pr_review_agent/agent/runner.py` — apertura RepoCheckout + binding tools per-run.
- `src/pr_review_agent/main.py` — passa `github_auth` al runner.
- `src/pr_review_agent/webhook.py` — popola `head_ref`/`head_sha` nello stato.
- `tests/unit/test_node_context_gatherer.py` — 7 test.
- `tests/unit/test_graph.py` — aggiornato per la nuova topologia.
