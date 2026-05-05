"""Custom exceptions for the GitHub integration domain.

Distinct types per concern (signature verification today; API errors and
auth failures in later tasks) so callers can match on intent instead of
swallowing every ``Exception``. Convention §10 in SPEC.md.
"""


class GitHubError(Exception):
    """Base for all GitHub-domain failures."""


class WebhookSignatureError(GitHubError):
    """Incoming webhook signature is missing, malformed, or does not match."""
