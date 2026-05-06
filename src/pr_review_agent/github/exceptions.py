"""Custom exceptions for the GitHub integration domain.

Distinct types per concern (signature verification today; API errors and
auth failures in later tasks) so callers can match on intent instead of
swallowing every ``Exception``. Convention §10 in SPEC.md.
"""


class GitHubError(Exception):
    """Base for all GitHub-domain failures."""


class WebhookSignatureError(GitHubError):
    """Incoming webhook signature is missing, malformed, or does not match."""


class GitHubAPIError(GitHubError):
    """GitHub API responded with an unexpected non-2xx status."""


class GitHubAuthError(GitHubError):
    """GitHub App JWT or installation token was rejected (401/403)."""


class GitHubNotFoundError(GitHubAPIError):
    """GitHub returned 404 for the requested resource.

    Subclass of ``GitHubAPIError`` so existing call sites that catch the
    base class still work; tools that want to skip missing resources
    (e.g. an issue referenced by ``Closes #42`` that no longer exists)
    catch this narrower type.
    """


class GitHubRateLimitError(GitHubAPIError):
    """GitHub primary or secondary rate limit was hit, or is about to be.

    Raised both reactively (the client saw 403/429 with ``retry-after``)
    and proactively (``X-RateLimit-Remaining`` is below the configured
    floor and ``X-RateLimit-Reset`` is too far away to sleep through).
    The runner catches this as a graceful abort path, mirroring
    ``CostCapExceeded``.

    ``retry_after_seconds`` is the suggested wait, when GitHub provided
    one; ``None`` means we inferred the limit ourselves and the caller
    can compute the wait from ``reset_epoch``.
    """

    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class RepoCloneError(GitHubError):
    """``git clone`` / ``git fetch`` / ``git checkout`` failed during checkout setup."""


class ToolPathError(Exception):
    """Filesystem tool was given a path that escapes the checkout root or doesn't exist.

    Lives outside ``GitHubError`` because it's about the agent's *tool
    contract*, not about GitHub itself: the LLM tried to read
    ``../../etc/passwd``, asked for a file that isn't there, or hit the
    bounded-read limit. Callers can surface this back to the model as a
    tool error message without confusing it with API failures.
    """
