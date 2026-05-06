"""Domain models shared by the agent tools.

``PRContext`` carries the per-PR coordinates every tool needs to read
from GitHub or from a local checkout: ``repo`` + ``pr_number`` +
``installation_id`` for the API surface, ``head_ref`` + ``head_sha``
for the git checkout. We bind it into the tool factory via closure
rather than passing it through LangChain's tool arguments — the LLM
should not be able to spoof which PR it is reviewing.

``LinkedIssue`` is the minimal projection of a GitHub Issue that the
agent actually reads. ``Match`` is the result shape returned by
``search_code``: file-relative path, line number, line text.
"""

from pydantic import BaseModel, Field


class PRContext(BaseModel):
    repo: str = Field(description="GitHub repo in 'owner/name' form")
    pr_number: int = Field(gt=0)
    installation_id: int = Field(gt=0)
    head_ref: str = Field(description="Branch name at the PR head, e.g. 'feat/foo'")
    head_sha: str = Field(description="Commit SHA at the PR head; pinned for review")


class LinkedIssue(BaseModel):
    number: int
    title: str
    body: str = ""
    state: str


class Match(BaseModel):
    path: str = Field(description="Repo-relative path, forward-slash separated")
    line: int = Field(gt=0)
    text: str = Field(description="The matching line, trimmed of trailing newline")
