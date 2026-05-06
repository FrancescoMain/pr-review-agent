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
