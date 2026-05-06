# pyright: reportUntypedFunctionDecorator=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""Convention-recall tool exposed to the Context Gatherer.

Wraps ``ConventionStore.query_conventions`` as a single LangChain
tool: ``recall_conventions(query)``. The ``repo`` and ``top_k`` are
captured in closure so the LLM can't pick them — same defensive
pattern as ``make_github_tools``.

If the per-repo collection doesn't exist (W3-Task4 ingest never ran
for this repo), the store returns an empty list and the LLM sees "no
matches" instead of an error. This keeps the bot useful on repos
that haven't been seeded yet.
"""

from langchain_core.tools import BaseTool, tool

from pr_review_agent.agent.memory.store import ConventionMatch, ConventionStore


def make_convention_tools(*, store: ConventionStore, repo: str, top_k: int = 5) -> list[BaseTool]:
    @tool
    async def recall_conventions(query: str) -> list[ConventionMatch]:
        """Search the project's convention memory (CLAUDE.md, README, docs/) for guidance.

        Use this when you suspect the project has a specific rule
        about something the diff touches — naming, testing, error
        handling, commit style, dependency policy, etc. ``query`` is
        a short natural-language phrase (e.g. "how should errors be
        raised?", "is there a convention for async tests?"). Returns
        up to a few matching chunks ordered by relevance, each with
        the source path and the chunk text. An empty list means the
        project has no specific guidance on the query — proceed with
        general best practices.
        """
        return await store.query_conventions(repo=repo, query=query, top_k=top_k)

    return [recall_conventions]
