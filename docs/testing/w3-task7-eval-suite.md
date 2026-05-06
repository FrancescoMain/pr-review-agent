# W3 · Task 7 — Eval suite su PR reali

## Cosa è stato consegnato

Un **eval harness** offline che rilancia l'agente su un dataset di PR pinnate (head_sha immutabile) e produce un report markdown. Per ogni PR il dataset descrive cosa la review dovrebbe catturare (`must_flag`) e cosa NON deve falsamente flaggare (`must_not_flag`); l'harness combina rule-based asserts con un giudizio LLM opzionale (Haiku-as-judge).

- **Dataset format**: `eval/dataset.yaml` validato da Pydantic (`EvalCase`, `ExpectedReview`, `MustFlagRule`, `MustNotFlagRule`). Selettori di severity: `"any"`, exact (`"issue"`), o `>=X` (`">=issue"` matcha issue + blocker).
- **Rule checkers** (`eval/asserts.py`):
  - `check_must_flag` — substring match case-insensitive, con verifica del severity selector. Per overall_comment (no severity carrier) solo `"any"` matcha.
  - `check_must_not_flag` — keyword nel review = fallimento.
  - `check_approval` / `check_skip` — confronto diretto con `ApprovalLevel` / `triage.should_skip`.
  - `evaluate_case` — orchestratore che li chiama tutti e ritorna la lista di failures.
- **LLM judge** (`eval/judge.py`): wrapper Haiku con `JudgeVerdict(score=1-5, rationale)`. Skippabile via `--no-judge`. Chain factory injectable per test offline.
- **CLI orchestrator** (`eval/run_eval.py`):
  - argparse: `--dataset`, `--out`, `--filter <id>`, `--no-judge`.
  - Per ogni case: costruisce un `AgentState` come fa il webhook, instanzia il runner reale ma con un **`_CapturingGitHubClient`** che cattura `post_pr_review`/`post_pr_comment` invece di POSTare. Il resto (`get_pr_diff`, `get_issue`, RepoCheckout) è reale.
  - Dopo il run: ricava `review` + `triage` da `final_state`, esegue le rule asserts, opzionalmente invoca il judge, accumula in `CaseResult`.
  - Render finale in markdown con tabella sintesi + dettaglio per-case + review JSON troncato.
  - Exit code: 1 se almeno un case fallisce (utile per CI futuro).
- **`Settings.github_default_installation_id: int | None`** per evitare di hardcodare l'installation_id reale nel YAML del dataset (il file resta committable senza secret leak).
- **Dataset seed** (3 entry sul playground): `pr-typo-readme` (skip path), `pr-divide-helper-bug` (real bug — quello del nostro smoke W2), `pr-large-refactor` (anti-hallucination check su rename). Due entry sono placeholder con SHA fasullo da aggiornare quando le PR esistono davvero.

**Cosa NON è stato fatto:**

- Cost tracking per case nel report — il runner usa il suo cost_callback interno e non lo espone. Lo aggiungiamo in W4 quando refattorizzeremo il runner per esporre i totals via state. Per ora il report mostra solo runtime + judge score.
- Dataset finale di 20 PR — solo 3 seed. Francesco aggiunge le altre quando vuole testare casi specifici.
- CI integration — il comando è manuale. In W4 deploy lo agganciamo a un nightly job.
- Comparazione tra run consecutivi (regression detection). Singolo run, singolo report.

## Setup dell'ambiente di test

Test pytest **completamente offline** (no Anthropic, no GitHub).

Per smoke E2E del CLI servono:
- `.env` con `ANTHROPIC_API_KEY`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_DEFAULT_INSTALLATION_ID` (o `installation_id` per-case nel YAML).
- Un dataset valido con SHA reali del playground.

## Scenari da testare manualmente

### 1 — Pytest verde

```bash
uv run pytest -q
```

Atteso: **227 passed** (204 dopo W3-Task6 + 6 nuovi `test_eval_dataset.py` + 14 `test_eval_asserts.py` + 3 `test_eval_judge.py`).

I nuovi test:

- **`test_eval_dataset.py` (6)**: minimal load, full load with rules, empty YAML, non-list root, short SHA, zero pr_number.
- **`test_eval_asserts.py` (14)**: must_flag con keyword in inline + overall, severity selectors esatti e `>=`, case-insensitive, must_not_flag, approval pass/fail/skip, skip with/without triage, evaluate_case combina tutto, evaluate_case con review=None.
- **`test_eval_judge.py` (3)**: factory wiring (input passa, verdict torna), score boundary 1-5, rejection score=0/score=6.

### 2 — Bruno collection verde (sanity check)

Niente endpoint nuovi.

### 3 — Smoke offline del CLI

Senza eseguire l'agente reale (richiederebbe Anthropic). Verifichiamo solo che la CLI carichi il dataset e produca un report degenerato:

```bash
uv run python -m eval.run_eval --filter pr-typo-readme --no-judge --out /tmp/eval-smoke
```

Atteso: il CLI proverà a istanziare il runner. Se mancano credenziali GitHub App / Anthropic, ogni case ritorna `CaseResult(error="...")` e il report markdown lo riporta correttamente. Exit code 1 (perché non passato).

```bash
cat /tmp/eval-smoke/report.md
```

Dovresti vedere la tabella con 1 riga ❌, error message specifico nel dettaglio.

### 4 — Smoke E2E con LLM reale

**Prerequisiti**:
- `.env` completo.
- Una vera PR aperta sul playground con head_sha noto. Aggiorna l'entry `pr-divide-helper-bug` in `eval/dataset.yaml` con il vero `head_sha` della PR #2.
- Postgres + Qdrant **non** richiesti (l'eval bypassa persistence).

```bash
uv run python -m eval.run_eval --filter pr-divide-helper-bug --no-judge
```

Atteso log:
- `eval.run_case case_id=pr-divide-helper-bug`
- (poi i log normali del run dell'agente: triage, gatherer, reviewer, critic, publisher con CapturingClient)
- `eval.report_written path=eval/reports/<isodate>/report.md`

Apri il report:

```bash
cat eval/reports/<isodate>/report.md
```

Dovresti vedere:
- ✅ se le rule pass (in particolare la flag su ZeroDivisionError)
- la review JSON troncata sotto "Per-case detail"
- runtime ~30-60s (gatherer + reviewer Sonnet)

### 5 — Smoke con judge attivo

```bash
uv run python -m eval.run_eval --filter pr-divide-helper-bug
```

(senza `--no-judge`). Atteso: stesso report di §4 più la colonna "judge" con uno score 1-5 + rationale Haiku. Costo extra: ~$0.001 per case.

### 6 — Tutto il dataset

```bash
uv run python -m eval.run_eval
```

Esegue tutti i case. Per i 2 placeholder con SHA fittizio (`pr-typo-readme`, `pr-large-refactor`) il run fallirà al checkout fase con `RepoCloneError` — il report li marca come errore. Atteso esit code 1.

Prima di girare l'eval completo: aggiorna i 2 placeholder con SHA reali, oppure cancella le 2 entry e tienile come template.

## Cosa cercare nei log / report

- `eval.run_case case_id=…` (info) — start di ogni case.
- `judge_failed case_id=… error=…` (warning) — il judge LLM ha errato la richiesta strutturata; il case continua, judge=None nel report.
- `eval.report_written path=…` (info) — file scritto a fine run.
- Nel report: `result=❌` con `summary` che dice `"N rule(s) failed"` o `"error: …"`. Il dettaglio per-case ha:
  - lista esplicita delle failures (es. `must_flag: no comment matched keyword='ZeroDivisionError' severity='>=issue'`),
  - JSON troncato della review,
  - score + rationale del judge se attivo.

## Limiti dichiarati

- **Niente cost tracking per case**. Refactor del runner per esporre `cost_cb.totals()` arriva W4. Per ora monitorare il costo totale del run dall'aggregato Anthropic dashboard.
- **Severity selector limitato a `>=X`**. Niente `<=X`, niente intervalli. Possiamo estendere se in futuro serve.
- **Match keyword case-insensitive ma niente regex / stemming**. "ZeroDivisionError" e "zero division error" non matchano: il dataset deve essere preciso.
- **Judge è 1 chiamata Haiku per case** — per dataset grandi (>20 PR) il costo del judge cresce. Skippabile via `--no-judge` se serve.
- **`installation_id` da `Settings.github_default_installation_id`** o per-case nel YAML. Il fallback è una concessione per la dev experience: lo metti in `.env` una volta e i case YAML restano portabili.
- **Niente parallelismo**: i case girano sequenziali. Volumi attuali (3 case) non lo richiedono.
- **CapturingClient** estende `GitHubClient` ma override solo i publish methods. Eredita lo stato di rate-limit; se qualcuno cambia internals di GitHubClient potrebbe rompere il client di eval — mitigato dai test esistenti che lo coprono indirettamente.

## Riferimenti file

- `eval/__init__.py` — module docstring.
- `eval/dataset.py` — Pydantic loader + schema.
- `eval/dataset.yaml` — 3 case seed.
- `eval/asserts.py` — rule checkers.
- `eval/judge.py` — LLM-as-judge wrapper.
- `eval/run_eval.py` — CLI orchestrator + report renderer + CapturingGitHubClient.
- `src/pr_review_agent/config.py` — `github_default_installation_id`.
- `tests/unit/test_eval_dataset.py` — 6 test.
- `tests/unit/test_eval_asserts.py` — 14 test.
- `tests/unit/test_eval_judge.py` — 3 test.
- `.gitignore` — esclude `eval/reports/`.
