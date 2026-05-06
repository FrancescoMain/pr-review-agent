# W2 · Task 5 — Pubblicazione review inline via Reviews API

## Cosa è stato consegnato

Il primo "vero" output del bot lato GitHub: il commento posato è ora una **PR review** con annotazioni ancorate a file/riga, non più un issue comment unico.

- **`GitHubClient.post_pr_review(...)`** — verb nuovo che chiama `POST /repos/{owner}/{repo}/pulls/{n}/reviews` con `commit_id` + `body` + `event` (`COMMENT` / `APPROVE` / `REQUEST_CHANGES`) + `comments[]` (path/line/side/body). 4xx → `GitHubAPIError`.
- **`pr_review_agent.github.diff_parser.parse_post_lines(diff)`** — parser minimo unified-diff che ritorna `{path: {linee post-PR ammissibili}}`. Solo righe `' '` (context) e `'+'` (added) contano sul lato `RIGHT`. File aggiunti via `--- /dev/null` sono inclusi; file deleted (`+++ /dev/null`) saltati. Hunk multipli per file gestiti.
- **Publisher con tre branch:**
  1. **`review` presente** → parsea `raw_diff` (persistito dal Reviewer, vedi sotto), splitta gli `inline_comments` in **anchorable** (linea nel diff) e **non-anchorable**. Il primo gruppo va inline su `POST /reviews`; il secondo è degradato a bullet list nel `body` con il prefisso `Comments not anchored to a changed line`. Su `GitHubAPIError` (es. 422 perché un anchor non era nel diff dopo tutto) → **fallback** automatico a `post_pr_comment` con il body completo.
  2. **`triage.should_skip == True`** (e niente review) → "Skipped review — triage classified this PR as ..." via `post_pr_comment`. Invariato rispetto a Task 4.
  3. **Fallback W1** (niente review, niente skip) → "Hello from agent — ..." via `post_pr_comment`.
- **`raw_diff: NotRequired[str | None]` in `AgentState`** — il **Reviewer** ora ritorna il diff in stato (lo aveva già fetcato per il prompt) così il Publisher non fa una terza GET. Cost-friendly e deterministico.
- **Mapping severity → markdown**: ogni inline body (sia inline che bullet) prefisso con glyph + `**[severity]**`. Es: `· **[nit]** prefer const`, `⚠️ **[issue]** off-by-one risk`, `🛑 **[blocker]** ...`, `💡 **[suggestion]** ...`.
- **Approval → event**: `approve` → `APPROVE`, `comment` → `COMMENT`, `request_changes` → `REQUEST_CHANGES`. Pin a `state["head_sha"]` su `commit_id`.

**Cosa NON è stato fatto:**

- Reazione a `head_sha` cambiato durante il run (force-push). 422 ⇒ fallback issue comment, è quello.
- Commenti su `LEFT` (riga rimossa). Sempre `RIGHT`. Non l'ho introdotto perché non è chiaro che sia mai utile in una review automatica.
- Update di review esistenti / dismissals / batch su PR sequenziali — fuori scope W2.

## Setup dell'ambiente di test

Nessuna dipendenza nuova. I test del diff parser e del publisher sono completamente offline.

## Scenari da testare manualmente

### 1 — Pytest verde

```bash
uv run pytest -q
```

Atteso: **108 passed** (95 dopo W2 Task 4 + 7 nuovi `test_diff_parser.py` + 6 nuovi/aggiornati in `test_node_publisher.py` rispetto ai 4 di Task 4 — qualcuno è stato sostituito/risistemato — vedi sotto).

Suddivisione dei nuovi test:

- **`test_diff_parser.py` (7)**:
  - file aggiunto da zero → tutte le righe ammissibili,
  - file modificato → solo context + added,
  - file deleted → vocabolario vuoto,
  - hunk multipli sullo stesso file → unione,
  - più file in un solo diff,
  - binary files (nessun `+++`) ignorati,
  - blank line in hunk = context.
- **`test_node_publisher.py` (10 totali)**: oltre ai test no-review (4 invariati), 6 nuovi sul Reviews path:
  - happy con anchorable + non-anchorable nello stesso review,
  - approval=approve → event=APPROVE,
  - approval=request_changes → event=REQUEST_CHANGES,
  - 422 da `post_pr_review` → fallback su `post_pr_comment` con body completo,
  - `raw_diff` mancante → tutti gli inline degradati nel body,
  - review + skip nello stesso state → review wins.
- **`test_github_client.py` (+2)**: payload corretto su `post_pr_review`; 422 propagato come `GitHubAPIError`.
- **`test_node_reviewer.py` (+1)**: il `update` del nodo include `raw_diff` con il valore di `get_pr_diff`.

### 2 — Bruno collection verde (sanity check)

Niente endpoint nuovi. Verifichiamo che la collection resti verde:

```bash
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  uv run uvicorn pr_review_agent.main:app --port 8001 &
until curl -sf http://localhost:8001/health > /dev/null; do sleep 0.2; done
GITHUB_WEBHOOK_SECRET=test-bruno-secret \
  bash -lc 'cd bruno && npx --yes @usebruno/cli run --env local'
kill %1
```

Atteso: stesso risultato di W2 Task 4.

### 3 — Diff parser via REPL

```bash
uv run python - <<'PY'
from pr_review_agent.github.diff_parser import parse_post_lines
diff = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,3 +1,4 @@
 a
+b
 c
 d
diff --git a/src/y.py b/src/y.py
--- a/src/y.py
+++ b/src/y.py
@@ -10,1 +10,1 @@
-old
+new
"""
print(parse_post_lines(diff))
# atteso: {'src/x.py': {1, 2, 3, 4}, 'src/y.py': {10}}
PY
```

### 4 — Publisher isolato via REPL (senza HTTP)

```bash
uv run python - <<'PY'
import asyncio
from pr_review_agent.agent.models import (
    ApprovalLevel, ChangeType, InlineComment, ReviewResult,
    RiskLevel, Severity, TriageDecision,
)
from pr_review_agent.agent.nodes.publisher import make_publisher_node
from pr_review_agent.github.exceptions import GitHubAPIError

class Recorder:
    def __init__(self): self.posted = []; self.reviews = []
    async def post_pr_comment(self, **kw): self.posted.append(kw)
    async def post_pr_review(self, **kw): self.reviews.append(kw)

state = {
    "repo":"x/y","pr_number":1,"installation_id":99,
    "head_ref":"feat/x","head_sha":"abc"+"0"*37,
    "triage":TriageDecision(change_type=ChangeType.feature, risk_level=RiskLevel.medium),
    "raw_diff":(
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -1,2 +1,3 @@\n a\n+b\n c\n"
    ),
    "review":ReviewResult(
        overall_comment="LGTM",
        inline_comments=[
            InlineComment(path="x.py", line=2, body="prefer const", severity=Severity.nit),
            InlineComment(path="x.py", line=99, body="non-anchored", severity=Severity.issue),
        ],
        approval=ApprovalLevel.comment,
    ),
}
client = Recorder()
node = make_publisher_node(client)
asyncio.run(node(state))
print("REVIEWS:", client.reviews)
print("COMMENTS:", client.posted)
PY
```

Atteso:

- `client.reviews` ha **1 entry**, con `event=COMMENT`, `commit_id="abc0000..."`, `comments=[{"path":"x.py","line":2,"side":"RIGHT","body":"· **[nit]** prefer const"}]`, e `body` che contiene `"x.py:99 — ⚠️ **[issue]** non-anchored"`.
- `client.posted` vuoto (no fallback).

### 5 — Verifica fallback su 422

Sostituisci nello snippet precedente la classe `Recorder` con:

```python
class Recorder:
    def __init__(self): self.posted = []; self.reviews = []
    async def post_pr_comment(self, **kw): self.posted.append(kw)
    async def post_pr_review(self, **kw):
        raise GitHubAPIError("HTTP 422")
```

Atteso: `client.reviews` vuoto (la chiamata fallisce), `client.posted` ha **1 entry** con il body completo (overall + bullet `**Inline findings:**`). `final_comment` nello state è il fallback body.

### 6 — End-to-end con LLM reale

Apri una PR sul playground con un file modificato in modo riconoscibile (es. typo in un README). Tail dei log:

```bash
uv run uvicorn pr_review_agent.main:app --reload
```

Cosa cercare:

- **Tab "Files changed"** della PR: dove il modello posiziona inline comment validi, vedi il glyph + `[severity]` + body. Sui non-anchored, vedi un summary nel commento di review.
- Nella tab "Conversation" della PR appare la review con `approval` (es. "Reviewer commented", "Reviewer requested changes").
- Se il Reviewer ha sbagliato i numeri di riga e GitHub risponde 422, nei log structlog vedi `review_api_failed_falling_back_to_issue_comment` e in PR appare invece un issue comment unico.

### 7 — Verifica `commit_id` pin

Per simulare force-push tra il fetch del diff e la post-review (raro ma possibile): in `runner.py` cambia temporaneamente `head_sha=state["head_sha"]` con `head_sha="0"*40` solo per la `_publish_review`. Atteso: GitHub risponde 422 ("commit not part of pull request"), il fallback fa partire il `post_pr_comment` con il body completo. Rimuovi la modifica.

## Cosa cercare nei log

- `review_api_failed_falling_back_to_issue_comment` con `error=...` → la Reviews API ha rifiutato il batch; il fallback è partito.
- `agent run failed` → tutto il run è morto a monte (Anthropic, clone, auth). Niente di Reviews API qui.

## Limiti dichiarati

- **Parser unified-diff è minimal**, riconosce GitHub diffs standard. Non gestisce diff "binari testuali" (es. JSON con marker custom), modalità `--git-dir-filter`, nessun supporto per il formato `diff -c` (combined). Per W2 il diff arriva sempre da GitHub Reviews API standard, OK.
- **`(path, line)` ammissibili sul lato `RIGHT` solo.** Non commentiamo su righe rimosse — voluto.
- **Fallback a issue comment è "all-or-nothing":** se Reviews API fallisce, posiamo l'intero review come issue comment, senza tentare un sub-batch ridotto. Aggiungere un retry più granulare è W3 se l'eval ce lo chiede.
- **Glyph emoji** (`🛑⚠️💡`) potrebbero non renderizzare in alcuni client GitHub Mobile vecchi. Il `[severity]` testuale resta sempre presente come fallback semantico.
- **Pin a `head_sha`**: se l'autore della PR force-pusha tra il fetch del diff (Gatherer/Reviewer) e la pubblicazione, l'API risponde 422. Fallback issue comment se ne occupa.

## Riferimenti file

- `src/pr_review_agent/github/diff_parser.py` — parser minimal.
- `src/pr_review_agent/github/client.py` — nuovo verbo `post_pr_review`.
- `src/pr_review_agent/agent/nodes/publisher.py` — branching su review presente / skip / hello + Reviews API + fallback.
- `src/pr_review_agent/agent/nodes/reviewer.py` — ritorna `raw_diff` in update.
- `src/pr_review_agent/agent/state.py` — campo nuovo `raw_diff`.
- `tests/unit/test_diff_parser.py` — 7 test.
- `tests/unit/test_node_publisher.py` — 10 test (4 no-review + 6 review path).
- `tests/unit/test_github_client.py` — 2 nuovi test.
- `tests/unit/test_node_reviewer.py` — assert su `raw_diff` in update.
