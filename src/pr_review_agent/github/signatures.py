"""HMAC SHA-256 verification of GitHub webhook payloads.

GitHub signs every webhook delivery with HMAC-SHA256 over the raw request
body, using the secret configured in the GitHub App. We compare in
constant time (``hmac.compare_digest``) to neutralise timing oracles.
Reference:
https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
"""

import hashlib
import hmac

from pr_review_agent.github.exceptions import WebhookSignatureError

_SIGNATURE_PREFIX = "sha256="


def verify_signature(body: bytes, signature_header: str | None, secret: str) -> None:
    if not signature_header:
        raise WebhookSignatureError("missing signature header")
    if not signature_header.startswith(_SIGNATURE_PREFIX):
        raise WebhookSignatureError("malformed signature header")
    received = signature_header.removeprefix(_SIGNATURE_PREFIX)
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received, expected):
        raise WebhookSignatureError("signature mismatch")
