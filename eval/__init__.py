"""Evaluation harness (W3-Task7).

Runs the agent against a curated dataset of pinned-SHA PRs and
produces a markdown report. Dataset entries describe what the
review *should* catch (``must_flag``) and what it must NOT
falsely flag (``must_not_flag``); the harness combines rule-based
checks with an optional Haiku-as-judge LLM score.

This is a manual tool — not part of pytest. Run with::

    uv run python -m eval.run_eval --filter pr-001 --no-judge
"""
