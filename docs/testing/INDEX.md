# Manual testing guides — index

Una guida per task. Ogni voce racconta cosa è stato consegnato, come testarlo a mano, e cosa aspettarsi (happy path **e** scenari di errore). Le testing guide sono complementari ai test pytest e alla Bruno collection: pytest copre la logica interna, Bruno copre il contratto HTTP, queste guide spiegano *cosa guardare* quando provi la feature di persona.

| Settimana | Task | Guida | Stato |
|---|---|---|---|
| 1 | 2 — FastAPI scaffold + Pydantic Settings + Bruno collection | [w1-task2-fastapi-scaffold.md](./w1-task2-fastapi-scaffold.md) | ✅ |
| 1 | 3 — Webhook signature verification + payload parsing | [w1-task3-webhook-security.md](./w1-task3-webhook-security.md) | ✅ |
| 1 | 4 — GitHub App auth (JWT + installation token + cache) | [w1-task4-github-app-auth.md](./w1-task4-github-app-auth.md) | ✅ |

## Convenzioni

- Nome file: `w<settimana>-task<numero>-<slug>.md` (es. `w1-task2-fastapi-scaffold.md`).
- Lingua: italiano. Tono: tecnico, narrativo, pensato per Francesco come revisore.
- Ogni guida segue la struttura: cosa è stato consegnato → setup → scenari numerati (happy + errore) → cosa cercare nei log → riferimenti file → limiti dichiarati.
