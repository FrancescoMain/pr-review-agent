"""End-to-end tests for ``RepoCheckout`` against a local file:// remote.

We init a real git repo in a tmpdir, commit a couple of files, and use
its path as the ``remote_url_override`` so the test never touches the
network. This catches regressions in our ``git init / fetch / checkout``
sequence (the kind of thing a unit test with mocks would silently miss),
keeps the suite offline, and runs in well under a second.
"""

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from pr_review_agent.agent.tools import PRContext, RepoCheckout
from pr_review_agent.github.exceptions import RepoCloneError


def _git(*args: str, cwd: Path) -> None:
    env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    subprocess.run(["git", *args], cwd=str(cwd), check=True, env=env, capture_output=True)


@pytest.fixture
def remote_repo(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    """Init a real git repo as 'remote' and return (path, head_sha)."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--quiet", "--initial-branch=main", cwd=remote)
    (remote / "README.md").write_text("hello\n", encoding="utf-8")
    (remote / "src").mkdir()
    (remote / "src" / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git("add", "-A", cwd=remote)
    _git("commit", "-m", "initial", "--quiet", cwd=remote)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(remote),
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    ).stdout.strip()
    # Allow fetching arbitrary sha from a non-bare repo.
    _git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=remote)
    yield remote, head_sha
    shutil.rmtree(remote, ignore_errors=True)


def _ctx(head_sha: str) -> PRContext:
    return PRContext(
        repo="francesco/playground",
        pr_number=1,
        installation_id=99,
        head_ref="main",
        head_sha=head_sha,
    )


async def test_checkout_materialises_head_sha(remote_repo: tuple[Path, str]) -> None:
    remote, head_sha = remote_repo
    async with RepoCheckout(ctx=_ctx(head_sha), remote_url_override=str(remote)) as checkout:
        assert checkout.root.exists()
        assert (checkout.root / "README.md").read_text() == "hello\n"
        assert (checkout.root / "src" / "app.py").read_text().startswith("def f():")
        # Sanity: the .git dir is there (search_code uses git grep).
        assert (checkout.root / ".git").is_dir()


async def test_checkout_cleans_up_on_exit(remote_repo: tuple[Path, str]) -> None:
    remote, head_sha = remote_repo
    async with RepoCheckout(ctx=_ctx(head_sha), remote_url_override=str(remote)) as checkout:
        root = checkout.root
        assert root.exists()
    assert not root.exists()


async def test_checkout_root_unavailable_outside_context(remote_repo: tuple[Path, str]) -> None:
    _, head_sha = remote_repo
    checkout = RepoCheckout(ctx=_ctx(head_sha), remote_url_override=str(remote_repo[0]))
    with pytest.raises(RuntimeError):
        _ = checkout.root


async def test_checkout_raises_on_missing_remote(tmp_path: Path) -> None:
    bogus = str(tmp_path / "does-not-exist")
    with pytest.raises(RepoCloneError):
        async with RepoCheckout(ctx=_ctx("0" * 40), remote_url_override=bogus):
            pass


async def test_checkout_raises_on_unknown_sha(remote_repo: tuple[Path, str]) -> None:
    remote, _ = remote_repo
    with pytest.raises(RepoCloneError):
        async with RepoCheckout(
            ctx=_ctx("deadbeef" * 5),  # 40 hex chars but not a real commit
            remote_url_override=str(remote),
        ):
            pass


async def test_checkout_cleanup_runs_even_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If git fetch fails, the tmpdir created by mkdtemp must not be left behind."""
    captured: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(prefix: str = "tmp") -> str:
        path = real_mkdtemp(prefix=prefix, dir=str(tmp_path))
        captured.append(path)
        return path

    monkeypatch.setattr(
        "pr_review_agent.agent.tools.repo_checkout.tempfile.mkdtemp",
        recording_mkdtemp,
    )

    with pytest.raises(RepoCloneError):
        async with RepoCheckout(ctx=_ctx("0" * 40), remote_url_override=str(tmp_path / "nope")):
            pass

    assert captured, "RepoCheckout did not call mkdtemp"
    assert not Path(captured[0]).exists()  # noqa: ASYNC240 — single check, tmpdir is local


def test_constructor_rejects_both_or_neither_auth_arg() -> None:
    """Exactly one of auth or remote_url_override must be provided."""
    ctx = _ctx("0" * 40)
    with pytest.raises(ValueError):
        RepoCheckout(ctx=ctx)
