from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

import httpx
import jwt

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_GITHUB_API_VERSION = "2022-11-28"
# Seconds to subtract from iat to tolerate minor clock skew between buma and GitHub.
_JWT_CLOCK_SKEW = 60
# Maximum JWT lifetime GitHub allows (seconds).
_JWT_LIFETIME = 600


class GitHubClient:
    """
    Async client for GitHub App API interactions required by Phase 6.

    Responsibilities:
    - Build a short-lived RS256 JWT from the App private key.
    - Exchange the JWT for a per-installation access token.
    - PATCH an issue (labels + assignee).
    - POST an explanation comment on an issue.

    A fresh installation token is requested per event — no caching in MVP.
    """

    def __init__(self, app_id: int, private_key_pem: str) -> None:
        self._app_id = app_id
        self._private_key_pem = private_key_pem.replace("\\n", "\n")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_installation_token(self, installation_id: int) -> str:
        """Exchange a signed JWT for a GitHub App installation access token."""
        token = self._build_jwt()
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{_GITHUB_API}/app/installations/{installation_id}/access_tokens",
                headers=self._jwt_headers(token),
            )
            response.raise_for_status()
            return response.json()["token"]

    async def patch_issue(
        self,
        installation_token: str,
        owner: str,
        repo: str,
        number: int,
        labels: list[str],
        assignee: str | None,
    ) -> None:
        """Apply labels and (optionally) an assignee to a GitHub issue."""
        body: dict = {"labels": labels}
        if assignee:
            body["assignees"] = [assignee]

        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{_GITHUB_API}/repos/{owner}/{repo}/issues/{number}",
                headers=self._token_headers(installation_token),
                json=body,
            )
            response.raise_for_status()

    async def post_comment(
        self,
        installation_token: str,
        owner: str,
        repo: str,
        number: int,
        body: str,
    ) -> None:
        """Post a comment on a GitHub issue."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{_GITHUB_API}/repos/{owner}/{repo}/issues/{number}/comments",
                headers=self._token_headers(installation_token),
                json={"body": body},
            )
            response.raise_for_status()

    async def list_issues(
        self,
        installation_token: str,
        owner: str,
        repo: str,
        state: str = "all",
        per_page: int = 100,
    ) -> AsyncIterator[dict]:
        """
        Yield every issue in a repository, following `Link: rel="next"` pagination.

        NOTE: GitHub's issues endpoint also returns pull requests (items with a `pull_request`
        key). They are yielded as-is — callers that want issues only must filter them out.
        """
        url: str | None = f"{_GITHUB_API}/repos/{owner}/{repo}/issues"
        params: dict | None = {"state": state, "per_page": per_page}
        async with httpx.AsyncClient() as client:
            while url:
                response = await client.get(url, headers=self._token_headers(installation_token), params=params)
                response.raise_for_status()
                for item in response.json():
                    yield item
                # The "next" URL already carries the query string.
                url = response.links.get("next", {}).get("url")
                params = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iss": str(self._app_id),
            "iat": now - _JWT_CLOCK_SKEW,
            "exp": now + _JWT_LIFETIME,
        }
        return jwt.encode(payload, self._private_key_pem, algorithm="RS256")

    @staticmethod
    def _jwt_headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        }

    @staticmethod
    def _token_headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        }
