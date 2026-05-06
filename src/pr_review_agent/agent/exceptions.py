"""Domain exceptions for agent guardrails.

These are *internal* exceptions: they signal that the agent
deliberately decided to stop, not that GitHub or the network failed.
The runner catches them and turns them into a graceful abort —
posting a brief explanation comment and recording the run's status —
without bubbling up as ``agent run failed``.
"""

from decimal import Decimal


class CostCapExceeded(Exception):
    """Raised when a run's accumulated USD cost exceeds the configured cap.

    The runner catches this, posts a "review aborted: cost cap reached"
    comment on the PR, and records the run with ``status='aborted_cost'``.
    """

    def __init__(self, *, current_cost: Decimal, cap: Decimal) -> None:
        super().__init__(f"cost cap exceeded: current=${current_cost:.6f} cap=${cap:.6f}")
        self.current_cost = current_cost
        self.cap = cap
