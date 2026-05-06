# Manual testing guides — index

Una guida per task. Ogni voce racconta cosa è stato consegnato, come testarlo a mano, e cosa aspettarsi (happy path **e** scenari di errore). Le testing guide sono complementari ai test pytest e alla Bruno collection: pytest copre la logica interna, Bruno copre il contratto HTTP, queste guide spiegano *cosa guardare* quando provi la feature di persona.

| Settimana | Task | Guida | Stato |
|---|---|---|---|
| 1 | 2 — FastAPI scaffold + Pydantic Settings + Bruno collection | [w1-task2-fastapi-scaffold.md](./w1-task2-fastapi-scaffold.md) | ✅ |
| 1 | 3 — Webhook signature verification + payload parsing | [w1-task3-webhook-security.md](./w1-task3-webhook-security.md) | ✅ |
| 1 | 4 — GitHub App auth (JWT + installation token + cache) | [w1-task4-github-app-auth.md](./w1-task4-github-app-auth.md) | ✅ |
| 1 | 5 — LangGraph hello-world + dispatch reale (triage → publisher) | [w1-task5-langgraph-hello-world.md](./w1-task5-langgraph-hello-world.md) | ✅ |
| 1 | 6 — Docker Compose (Postgres + Qdrant) | [w1-task6-docker-compose.md](./w1-task6-docker-compose.md) | ✅ |
| 1 | 7 — LangSmith tracing + GitHub Actions CI | [w1-task7-langsmith-and-ci.md](./w1-task7-langsmith-and-ci.md) | ✅ |
| 2 | 1 — Tool GitHub-side (`get_pr_diff`, `get_linked_issues`) | [w2-task1-github-tools.md](./w2-task1-github-tools.md) | ✅ |
| 2 | 2 — Tool filesystem (`read_file`, `list_directory`, `search_code`) + `RepoCheckout` | [w2-task2-filesystem-tools.md](./w2-task2-filesystem-tools.md) | ✅ |
| 2 | 3 — Nodo Context Gatherer (sub-grafo `model_step` + `tool_step`) | [w2-task3-context-gatherer.md](./w2-task3-context-gatherer.md) | ✅ |
| 2 | 4 — Nodo Reviewer + skip-route post-triage | [w2-task4-reviewer.md](./w2-task4-reviewer.md) | ✅ |
| 2 | 5 — Pubblicazione review inline via Reviews API | [w2-task5-inline-review.md](./w2-task5-inline-review.md) | ✅ |
| 2 | 6 — Correlation ID nei log strutturati | [w2-task6-correlation-id.md](./w2-task6-correlation-id.md) | ✅ |
| 2 | 7 — Cost tracking persistito su Postgres | [w2-task7-cost-tracking.md](./w2-task7-cost-tracking.md) | ✅ |
| 3 | 1 — Cost cap per PR (abort live + record `aborted_cost`) | [w3-task1-cost-cap.md](./w3-task1-cost-cap.md) | ✅ |
| 3 | 2 — Idempotency su `X-GitHub-Delivery` | [w3-task2-idempotency.md](./w3-task2-idempotency.md) | ✅ |
| 3 | 3 — Rate limit GitHub API (sleep / abort) | [w3-task3-rate-limit.md](./w3-task3-rate-limit.md) | ✅ |

## Convenzioni

- Nome file: `w<settimana>-task<numero>-<slug>.md` (es. `w1-task2-fastapi-scaffold.md`).
- Lingua: italiano. Tono: tecnico, narrativo, pensato per Francesco come revisore.
- Ogni guida segue la struttura: cosa è stato consegnato → setup → scenari numerati (happy + errore) → cosa cercare nei log → riferimenti file → limiti dichiarati.
