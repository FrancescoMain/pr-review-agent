"""Per-PR shallow git checkout used by the filesystem tools.

Strategy C from the W2 plan: clone-on-demand into a tmpdir scoped to a
single graph run. The async context manager ``RepoCheckout`` does
``git init`` → ``git fetch --depth=1 origin <head_sha>`` →
``git checkout FETCH_HEAD``, so we materialise *only* the head commit
of the PR — no history, no other branches, no tags. Auth is the
GitHub App installation token injected into the remote URL as
``https://x-access-token:<token>@github.com/<repo>.git`` (the
documented way for App-authenticated clones).

Cleanup is ``shutil.rmtree`` on ``__aexit__``: the tmpdir is gone
whether the run succeeded or threw. Tests use a real local git repo as
remote (``file://...``) so the suite stays offline.
"""

import asyncio
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Self

from pr_review_agent.agent.tools.models import PRContext
from pr_review_agent.github.auth import GitHubAppAuth
from pr_review_agent.github.exceptions import RepoCloneError

_GITHUB_REMOTE_TEMPLATE = "https://x-access-token:{token}@github.com/{repo}.git"


class RepoCheckout:
    def __init__(
        self,
        *,
        ctx: PRContext,
        auth: GitHubAppAuth | None = None,
        remote_url_override: str | None = None,
    ) -> None:
        """Either ``auth`` (production: builds an https URL with the installation token)
        or ``remote_url_override`` (tests: a ``file://`` URL or another local path).
        Exactly one of the two must be supplied; the constructor enforces it lazily
        so test fixtures don't have to fabricate a fake ``GitHubAppAuth``.
        """
        if (auth is None) == (remote_url_override is None):
            raise ValueError("RepoCheckout requires exactly one of 'auth' or 'remote_url_override'")
        self._ctx = ctx
        self._auth = auth
        self._remote_url_override = remote_url_override
        self._tmpdir: Path | None = None

    @property
    def root(self) -> Path:
        if self._tmpdir is None:
            raise RuntimeError("RepoCheckout used outside an 'async with' block")
        return self._tmpdir

    async def __aenter__(self) -> Self:
        remote = await self._build_remote_url()
        tmpdir = Path(tempfile.mkdtemp(prefix="pr-review-checkout-"))
        try:
            await self._run_git(["init", "--quiet"], cwd=tmpdir)
            await self._run_git(
                ["fetch", "--depth=1", "--no-tags", remote, self._ctx.head_sha],
                cwd=tmpdir,
            )
            await self._run_git(["checkout", "--quiet", "FETCH_HEAD"], cwd=tmpdir)
        except Exception:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        self._tmpdir = tmpdir
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    async def _build_remote_url(self) -> str:
        if self._remote_url_override is not None:
            return self._remote_url_override
        assert self._auth is not None  # guaranteed by __init__
        token = await self._auth.get_installation_token(self._ctx.installation_id)
        return _GITHUB_REMOTE_TEMPLATE.format(token=token.token, repo=self._ctx.repo)

    @staticmethod
    async def _run_git(args: Sequence[str], *, cwd: Path) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            # Don't leak the remote URL (it may contain a token); show args only up to the URL.
            safe_args = [a if not a.startswith("https://") else "<remote-url>" for a in args]
            raise RepoCloneError(
                f"git {' '.join(safe_args)} failed with exit {proc.returncode}: "
                f"{stderr.decode('utf-8', errors='replace').strip()}"
            )
