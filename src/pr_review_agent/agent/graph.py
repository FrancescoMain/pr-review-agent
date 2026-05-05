"""LangGraph graph definition.

Wires the five nodes (triage → context_gatherer → reviewer → critic →
publisher) with the retry edge from critic back to reviewer (max 1 retry).
See SPEC.md §3 for the full topology. Implementation in Week 1, Task 4.
"""
