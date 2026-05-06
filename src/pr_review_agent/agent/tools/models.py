"""Domain models shared by the agent tools.

``PRContext`` carries the per-PR coordinates (owner/repo/pr_number/
installation_id) that every tool needs to talk to GitHub. We bind it
into the tool factory via closure rather than passing it through
LangChain's tool arguments — the LLM should not be able to spoof which
PR it is reviewing.

``LinkedIssue`` is the minimal projection of a GitHub Issue that the
agent actually reads (number, title, body, state). The wire schema has
many more fields; we keep only what informs review decisions.
"""

from pydantic import BaseModel, Field


class PRContext(BaseModel):
    repo: str = Field(description="GitHub repo in 'owner/name' form")
    pr_number: int = Field(gt=0)
    installation_id: int = Field(gt=0)


class LinkedIssue(BaseModel):
    number: int
    title: str
    body: str = ""
    state: str
