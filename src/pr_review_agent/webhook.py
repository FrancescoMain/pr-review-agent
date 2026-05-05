"""GitHub webhook handler.

Verifies ``X-Hub-Signature-256`` (HMAC SHA256) against the configured webhook
secret, parses the ``pull_request`` event payload, and dispatches the agent
run as a background task. See SPEC.md §5. Implementation in Week 1, Task 3.
"""
