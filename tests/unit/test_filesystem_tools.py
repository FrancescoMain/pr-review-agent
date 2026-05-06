# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""Unit tests for the filesystem tools.

We build a real on-disk fixture (init'd as a git repo, because
``search_code`` shells out to ``git grep``) and pass its path to
``make_filesystem_tools``. This covers the actual subprocess wiring of
git grep — mocking that layer would be the exact same kind of blind
spot we just fixed for the triage prompt template.
"""

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from langchain_core.tools import BaseTool

from pr_review_agent.agent.tools import Match, make_filesystem_tools
from pr_review_agent.agent.tools.filesystem_tools import (
    MAX_FILE_BYTES,
    MAX_LIST_ENTRIES,
    MAX_SEARCH_MATCHES,
)
from pr_review_agent.github.exceptions import ToolPathError


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
def repo(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "checkout"
    root.mkdir()
    _git("init", "--quiet", "--initial-branch=main", cwd=root)
    (root / "README.md").write_text("hello world\nsecond line\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text(
        "def greet():\n    return 'hello world'\n\ndef bye():\n    return 'bye'\n",
        encoding="utf-8",
    )
    (root / "src" / "util.py").write_text("# helper\nVERSION = '1.0'\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "evil.txt").write_text("should be hidden", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "init", "--quiet", cwd=root)
    yield root


def _find_tool(tools: list[BaseTool], name: str) -> BaseTool:
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool '{name}' not in {[t.name for t in tools]}")


# ---------------------------- read_file ----------------------------


async def test_read_file_returns_utf8_content(repo: Path) -> None:
    tools = make_filesystem_tools(repo)
    result = await _find_tool(tools, "read_file").ainvoke({"path": "README.md"})
    assert "hello world" in result
    assert "second line" in result


async def test_read_file_rejects_path_traversal(repo: Path) -> None:
    tools = make_filesystem_tools(repo)
    with pytest.raises(ToolPathError):
        await _find_tool(tools, "read_file").ainvoke({"path": "../etc/passwd"})


async def test_read_file_rejects_absolute_path(repo: Path) -> None:
    tools = make_filesystem_tools(repo)
    with pytest.raises(ToolPathError):
        await _find_tool(tools, "read_file").ainvoke({"path": "/etc/passwd"})


async def test_read_file_missing_file(repo: Path) -> None:
    tools = make_filesystem_tools(repo)
    with pytest.raises(ToolPathError):
        await _find_tool(tools, "read_file").ainvoke({"path": "src/nope.py"})


async def test_read_file_rejects_directory(repo: Path) -> None:
    tools = make_filesystem_tools(repo)
    with pytest.raises(ToolPathError):
        await _find_tool(tools, "read_file").ainvoke({"path": "src"})


async def test_read_file_truncates_oversize(repo: Path) -> None:
    big = repo / "big.txt"
    big.write_text("x" * (MAX_FILE_BYTES + 1024), encoding="utf-8")
    tools = make_filesystem_tools(repo)
    result = await _find_tool(tools, "read_file").ainvoke({"path": "big.txt"})
    assert "[... truncated:" in result
    # Body content is bounded; the marker is appended *after* the cap.
    assert result.count("x") == MAX_FILE_BYTES


# ---------------------------- list_directory ----------------------------


async def test_list_directory_root(repo: Path) -> None:
    tools = make_filesystem_tools(repo)
    entries = await _find_tool(tools, "list_directory").ainvoke({"path": "."})
    assert "README.md" in entries
    assert "src/" in entries
    # Skip-dir filter eats node_modules, .git
    assert "node_modules/" not in entries
    assert ".git/" not in entries


async def test_list_directory_subdir(repo: Path) -> None:
    tools = make_filesystem_tools(repo)
    entries = await _find_tool(tools, "list_directory").ainvoke({"path": "src"})
    assert sorted(entries) == ["app.py", "util.py"]


async def test_list_directory_traversal_rejected(repo: Path) -> None:
    tools = make_filesystem_tools(repo)
    with pytest.raises(ToolPathError):
        await _find_tool(tools, "list_directory").ainvoke({"path": "../"})


async def test_list_directory_missing_dir(repo: Path) -> None:
    tools = make_filesystem_tools(repo)
    with pytest.raises(ToolPathError):
        await _find_tool(tools, "list_directory").ainvoke({"path": "no/such/dir"})


async def test_list_directory_caps_at_max_entries(repo: Path) -> None:
    big = repo / "many"
    big.mkdir()
    for i in range(MAX_LIST_ENTRIES + 50):
        (big / f"f{i:04d}.txt").write_text("", encoding="utf-8")
    tools = make_filesystem_tools(repo)
    entries = await _find_tool(tools, "list_directory").ainvoke({"path": "many"})
    assert len(entries) == MAX_LIST_ENTRIES + 1  # +1 for the truncation marker
    assert entries[-1].startswith("[... truncated")


# ---------------------------- search_code ----------------------------


async def test_search_code_finds_matches(repo: Path) -> None:
    tools = make_filesystem_tools(repo)
    matches = await _find_tool(tools, "search_code").ainvoke({"query": "hello world"})
    paths = sorted({m.path for m in matches})
    assert paths == ["README.md", "src/app.py"]
    for m in matches:
        assert isinstance(m, Match)
        assert m.line > 0


async def test_search_code_no_matches_returns_empty(repo: Path) -> None:
    tools = make_filesystem_tools(repo)
    matches = await _find_tool(tools, "search_code").ainvoke({"query": "needle-not-here"})
    assert matches == []


async def test_search_code_file_pattern_filters(repo: Path) -> None:
    tools = make_filesystem_tools(repo)
    matches = await _find_tool(tools, "search_code").ainvoke(
        {"query": "hello world", "file_pattern": "*.py"}
    )
    assert {m.path for m in matches} == {"src/app.py"}


async def test_search_code_rejects_empty_query(repo: Path) -> None:
    tools = make_filesystem_tools(repo)
    with pytest.raises(ToolPathError):
        await _find_tool(tools, "search_code").ainvoke({"query": ""})


async def test_search_code_caps_at_max_matches(repo: Path) -> None:
    spammy = repo / "spammy.txt"
    spammy.write_text("\n".join(["needle"] * (MAX_SEARCH_MATCHES + 20)), encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "spammy", "--quiet", cwd=repo)
    tools = make_filesystem_tools(repo)
    matches = await _find_tool(tools, "search_code").ainvoke({"query": "needle"})
    assert len(matches) == MAX_SEARCH_MATCHES
