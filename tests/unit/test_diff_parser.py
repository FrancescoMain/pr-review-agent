"""Tests for the unified-diff parser used by the Publisher.

Each test feeds a small fragment that we can read by eye and asserts
exactly which ``(path, line)`` pairs are eligible for inline review
comments — i.e. lines that actually exist on the right side of the diff.
"""

from pr_review_agent.github.diff_parser import parse_post_lines


def test_parse_added_file_lists_every_line() -> None:
    diff = (
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+def f():\n"
        "+    return 1\n"
        "+\n"
    )
    assert parse_post_lines(diff) == {"new.py": {1, 2, 3}}


def test_parse_modified_file_counts_added_and_context_only() -> None:
    diff = (
        "diff --git a/x.py b/x.py\n"
        "index abc..def 100644\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -10,3 +10,4 @@\n"
        " context_line_a\n"
        "-old_line\n"
        "+new_line_1\n"
        "+new_line_2\n"
        " context_line_b\n"
    )
    # Right side: line 10 (context), 11 (added), 12 (added), 13 (context).
    assert parse_post_lines(diff) == {"x.py": {10, 11, 12, 13}}


def test_parse_deleted_file_has_no_right_side_entry() -> None:
    diff = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n"
        "-line one\n"
        "-line two\n"
        "-line three\n"
    )
    assert parse_post_lines(diff) == {}


def test_parse_multiple_hunks_in_same_file() -> None:
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,2 +1,3 @@\n"
        " a\n"
        "+b\n"
        " c\n"
        "@@ -50,1 +51,2 @@\n"
        " d\n"
        "+e\n"
    )
    assert parse_post_lines(diff) == {"x.py": {1, 2, 3, 51, 52}}


def test_parse_handles_multiple_files() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1,2 @@\n"
        " same\n"
        "+added\n"
        "diff --git a/b.py b/b.py\n"
        "--- a/b.py\n"
        "+++ b/b.py\n"
        "@@ -5 +5 @@\n"
        "-old\n"
        "+new\n"
    )
    assert parse_post_lines(diff) == {"a.py": {1, 2}, "b.py": {5}}


def test_parse_ignores_binary_files() -> None:
    diff = "diff --git a/img.png b/img.png\nBinary files a/img.png and b/img.png differ\n"
    assert parse_post_lines(diff) == {}


def test_parse_blank_line_in_hunk_counts_as_context() -> None:
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n\n c\n"
    # Blank line at line 2 still occupies a right-side line number.
    assert parse_post_lines(diff) == {"x.py": {1, 2, 3}}
