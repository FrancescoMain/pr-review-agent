"""Pydantic models for the subset of GitHub webhook payloads we consume.

Only the fields the agent actually reads are declared. Adding more is
cheap; carrying around fields nobody uses creates brittle coupling to
GitHub's evolving API surface.
"""

from pydantic import BaseModel, ConfigDict


class Repository(BaseModel):
    model_config = ConfigDict(extra="ignore")

    full_name: str


class GitRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ref: str
    sha: str


class PullRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int
    title: str
    head: GitRef
    base: GitRef


class Installation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int


class PullRequestEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str
    number: int
    pull_request: PullRequest
    repository: Repository
    installation: Installation
