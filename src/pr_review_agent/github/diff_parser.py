"""Minimal unified-diff parser for the Publisher.

The GitHub Reviews API rejects the *whole* review with HTTP 422 if any
inline comment points to a line that the diff didn't actually touch on
the right (post-PR) side. We can't recover from that gracefully — the
review is atomic — so we filter inline comments *before* posting,
keeping the anchored ones inline and degrading the rest to bullet
points in the body.

``parse_post_lines`` returns the set of right-side line numbers we are
allowed to comment on, per file. We keep the parser deliberately
small: hunk headers (``@@ -X,Y +A,B @@``) tell us the post-PR start
line, then we walk the body counting context and added lines (``+``,
`` ``) and skipping deleted lines (``-``). Binary files, file renames
without changes, and ``--- /dev/null`` (file added) are all handled by
deriving the path from the ``+++ b/<path>`` line — when GitHub serves
``+++ /dev/null`` for a deleted file we just skip it (no right side
to comment on).
"""

import re

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_post_lines(diff: str) -> dict[str, set[int]]:
    """Return ``{path: {right_side_line_numbers}}`` for files with right-side content.

    ``path`` is the post-PR path stripped of the conventional ``b/``
    prefix. Files added in the PR are included. Files deleted in the
    PR are not (their ``+++`` line is ``/dev/null``).
    """
    result: dict[str, set[int]] = {}
    current_path: str | None = None
    line_no = 0

    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            current_path = _post_path_from_target(target)
            line_no = 0
            continue
        if current_path is None:
            continue
        if raw.startswith("@@"):
            match = _HUNK_RE.match(raw)
            if match is None:
                continue
            line_no = int(match.group(1))
            continue
        if raw.startswith("--- "):
            # New file header pair starts; reset until we see the matching +++.
            current_path = None
            continue
        if not raw:
            # Blank line inside a hunk counts as a context line.
            result.setdefault(current_path, set()).add(line_no)
            line_no += 1
            continue
        head = raw[0]
        if head == "+" or head == " ":
            result.setdefault(current_path, set()).add(line_no)
            line_no += 1
        elif head == "-":
            # Deletion: not on the right side, do not advance.
            pass
        else:
            # Diff metadata (``diff --git``, ``index``, ``Binary files``…) — ignore.
            continue

    return result


def _post_path_from_target(target: str) -> str | None:
    """Strip ``b/`` prefix from the ``+++`` line target, or return None for /dev/null."""
    if target == "/dev/null":
        return None
    # Strip optional timestamp after a tab (some diff tools include one).
    target = target.split("\t", 1)[0]
    if target.startswith("b/"):
        return target[2:]
    return target
