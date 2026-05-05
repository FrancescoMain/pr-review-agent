"""FastAPI application entry point.

Will expose ``/health``, ``/webhook/github`` and ``/metrics``. The webhook
handler returns 200 immediately and schedules the agent run as a background
task. See SPEC.md §5. Implementation lands in Week 1, Task 2.
"""
