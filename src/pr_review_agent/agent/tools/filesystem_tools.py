# pyright: reportUntypedFunctionDecorator=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""Filesystem-side tools backed by a ``RepoCheckout``.

W2-Task2 introduces the three tools the Context Gatherer will use to
read code: ``read_file`` (bounded read of a single file),
``list_directory`` (children of a directory, dot-dirs and vendored
trees skipped), ``search_code`` (``git grep`` over the checkout). All
three live behind ``make_filesystem_tools(checkout_root)`` and refuse paths
that escape the checkout root via ``ToolPathError`` — the LLM picks the
arguments, so we must treat them as untrusted.

Bounds (constants below) keep tool output small enough that the model
can reason about it without context-window blowups, and bounded enough
that a hostile input can't make us read a 1 GB file.
"""

import asyncio
from collections.abc import Sequence
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from pr_review_agent.agent.tools.models import Match
from pr_review_agent.github.exceptions import ToolPathError

MAX_FILE_BYTES = 200 * 1024
MAX_LIST_ENTRIES = 200
MAX_SEARCH_MATCHES = 50

# Directories the tools skip when listing/searching: VCS metadata, language
# package caches, build outputs. Keeps tool output focused on source code
# the agent can actually reason about.
SKIP_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".mypy_cache"}
)


def _safe_resolve(root: Path, user_path: str) -> Path:
    """Resolve ``user_path`` under ``root`` or raise ``ToolPathError``.

    ``..`` segments and absolute paths are rejected: the candidate must
    remain strictly inside the checkout root after resolution.
    """
    if not user_path or user_path == ".":
        return root.resolve()
    if Path(user_path).is_absolute():
        raise ToolPathError(f"absolute paths are not allowed: {user_path!r}")
    candidate = (root / user_path).resolve()
    root_resolved = root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ToolPathError(f"path escapes checkout root: {user_path!r}") from exc
    return candidate


def make_filesystem_tools(checkout_root: Path) -> list[BaseTool]:
    """Build the three filesystem tools rooted at ``checkout_root``.

    Decoupled from ``RepoCheckout`` itself so tests can pass a tmpdir
    without spinning up a clone; the production runner does
    ``make_filesystem_tools(checkout.root)`` once the checkout has
    materialised.
    """

    @tool
    async def read_file(path: str) -> str:
        """Read a UTF-8 file from the PR checkout, bounded to a few hundred KB.

        ``path`` is repo-relative (forward slashes). The tool refuses
        absolute paths or anything that escapes the checkout root.
        Files larger than the cap are returned truncated with a clear
        marker; binary bytes are decoded with replacement characters so
        the model gets *something* useful even on misnamed binaries.
        """
        target = _safe_resolve(checkout_root, path)
        if not target.exists():
            raise ToolPathError(f"file not found: {path!r}")
        if not target.is_file():
            raise ToolPathError(f"not a regular file: {path!r}")
        size = target.stat().st_size
        with target.open("rb") as fh:
            payload = fh.read(MAX_FILE_BYTES)
        text = payload.decode("utf-8", errors="replace")
        if size > MAX_FILE_BYTES:
            text += (
                f"\n\n[... truncated: file is {size} bytes, only first {MAX_FILE_BYTES} read ...]"
            )
        return text

    @tool
    async def list_directory(path: str) -> list[str]:
        """List the children of a directory in the PR checkout.

        ``path`` is repo-relative (use ``"."`` for the root). Directories
        are returned with a trailing ``/`` so the model can tell them
        apart from files. VCS metadata, package caches, and build
        outputs are skipped. The result is bounded; if a directory has
        more than the cap, the list is truncated with a marker entry.
        """
        target = _safe_resolve(checkout_root, path)
        if not target.exists():
            raise ToolPathError(f"directory not found: {path!r}")
        if not target.is_dir():
            raise ToolPathError(f"not a directory: {path!r}")
        entries: list[str] = []
        for child in sorted(target.iterdir(), key=lambda p: p.name):
            if child.is_dir() and child.name in SKIP_DIRS:
                continue
            entries.append(f"{child.name}/" if child.is_dir() else child.name)
            if len(entries) >= MAX_LIST_ENTRIES:
                entries.append(f"[... truncated at {MAX_LIST_ENTRIES} entries ...]")
                break
        return entries

    @tool
    async def search_code(query: str, file_pattern: str | None = None) -> list[Match]:
        """Search the PR checkout for a literal substring via ``git grep``.

        ``query`` is matched as a fixed string (no regex). Optional
        ``file_pattern`` is a git pathspec / glob (e.g. ``"*.py"``) to
        restrict the search. Results are capped; over-cap, the list is
        truncated silently — refine the query if you hit the cap. The
        tool returns an empty list when there are no matches.
        """
        if not query:
            raise ToolPathError("search query must be non-empty")
        args: list[str] = ["grep", "-n", "--fixed-strings", "--no-color", "-e", query]
        if file_pattern:
            args.extend(["--", file_pattern])
        return await _run_git_grep(checkout_root, args)

    return [read_file, list_directory, search_code]


async def _run_git_grep(cwd: Path, args: Sequence[str]) -> list[Match]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    # git grep: 0 = matches, 1 = no matches, >1 = error.
    if proc.returncode == 1:
        return []
    if proc.returncode != 0:
        raise ToolPathError(f"git grep failed with exit {proc.returncode}")
    matches: list[Match] = []
    for raw in stdout.decode("utf-8", errors="replace").splitlines():
        # Format: "path:line:text" — but text may itself contain ':'.
        try:
            path, line_str, text = raw.split(":", 2)
            line = int(line_str)
        except ValueError:
            continue
        matches.append(Match(path=path, line=line, text=text))
        if len(matches) >= MAX_SEARCH_MATCHES:
            break
    return matches
