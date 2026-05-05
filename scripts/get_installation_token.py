"""List all installations of the configured GitHub App and print a fresh
installation token for each one.

This is a manual smoke against the *real* GitHub API; nothing in the
production flow runs it. Useful for verifying that the App ID and the
private key in your .env are wired correctly before opening a PR on
the playground repo.

Usage:
    uv run python scripts/get_installation_token.py
    uv run python scripts/get_installation_token.py --full   # don't mask
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from pr_review_agent.config import Settings
from pr_review_agent.github.auth import GitHubAppAuth

GITHUB_INSTALLATIONS_URL = "https://api.github.com/app/installations"


def _mask(token: str) -> str:
    if len(token) < 16:
        return "***"
    return f"{token[:8]}...{token[-4:]}"


async def _run(*, full: bool) -> int:
    settings = Settings()
    if (
        settings.github_app_id <= 0
        or settings.github_app_private_key_path is None
        or not settings.github_app_private_key_path.exists()
    ):
        sys.stderr.write(
            "GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH must be set in .env "
            "and the .pem file must exist.\n"
        )
        return 2

    pem = settings.github_app_private_key_path.read_text()

    async with httpx.AsyncClient(timeout=10.0) as http:
        auth = GitHubAppAuth(app_id=settings.github_app_id, private_key=pem, http_client=http)
        response = await http.get(
            GITHUB_INSTALLATIONS_URL,
            headers={
                "Authorization": f"Bearer {auth.build_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        installations = response.json()
        if not installations:
            sys.stdout.write(
                "No installations found. Install the GitHub App on at least one "
                "repository, then re-run.\n"
            )
            return 0
        for installation in installations:
            iid = installation["id"]
            account = installation.get("account", {}).get("login", "?")
            token = await auth.get_installation_token(iid)
            displayed = token.token if full else _mask(token.token)
            sys.stdout.write(
                f"installation_id={iid}  account={account}  "
                f"expires_at={token.expires_at.isoformat()}  token={displayed}\n"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Print the full installation token instead of a masked preview.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(full=args.full))


if __name__ == "__main__":
    sys.exit(main())
