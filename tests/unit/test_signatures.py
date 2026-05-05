"""Unit tests for ``pr_review_agent.github.signatures.verify_signature``.

We test the contract directly (no FastAPI in the loop) so that the
HMAC/constant-time logic is covered independently of the routing layer.
"""

import hashlib
import hmac

import pytest

from pr_review_agent.github.exceptions import WebhookSignatureError
from pr_review_agent.github.signatures import verify_signature


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_accepts_valid_signature() -> None:
    body = b'{"action":"opened"}'
    secret = "topsecret"
    verify_signature(body, _sign(body, secret), secret)


def test_verify_signature_rejects_missing_header() -> None:
    with pytest.raises(WebhookSignatureError):
        verify_signature(b"{}", None, "any")


def test_verify_signature_rejects_empty_header() -> None:
    with pytest.raises(WebhookSignatureError):
        verify_signature(b"{}", "", "any")


def test_verify_signature_rejects_missing_prefix() -> None:
    body = b"{}"
    secret = "topsecret"
    raw_hex = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    with pytest.raises(WebhookSignatureError):
        verify_signature(body, raw_hex, secret)


def test_verify_signature_rejects_tampered_body() -> None:
    secret = "topsecret"
    signature = _sign(b'{"action":"opened"}', secret)
    with pytest.raises(WebhookSignatureError):
        verify_signature(b'{"action":"closed"}', signature, secret)


def test_verify_signature_rejects_wrong_secret() -> None:
    body = b"{}"
    signature = _sign(body, "the-real-secret")
    with pytest.raises(WebhookSignatureError):
        verify_signature(body, signature, "different-secret")
