from __future__ import annotations

import time

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from buma.worker.services.github_client import _GITHUB_API, GitHubClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_private_key_pem() -> str:
    """Generate a throwaway RSA key for tests — not used against real GitHub."""
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def client(rsa_private_key_pem: str) -> GitHubClient:
    return GitHubClient(app_id=12345, private_key_pem=rsa_private_key_pem)


# ---------------------------------------------------------------------------
# JWT construction
# ---------------------------------------------------------------------------


def test_build_jwt_is_rs256(client: GitHubClient, rsa_private_key_pem: str) -> None:
    token = client._build_jwt()
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"


def test_build_jwt_iss_is_app_id(client: GitHubClient, rsa_private_key_pem: str) -> None:
    token = client._build_jwt()
    # Decode without verification to inspect payload
    payload = jwt.decode(token, options={"verify_signature": False})
    assert payload["iss"] == "12345"


def test_build_jwt_exp_is_in_future(client: GitHubClient) -> None:
    token = client._build_jwt()
    payload = jwt.decode(token, options={"verify_signature": False})
    assert payload["exp"] > int(time.time())


def test_build_jwt_iat_accounts_for_clock_skew(client: GitHubClient) -> None:
    token = client._build_jwt()
    payload = jwt.decode(token, options={"verify_signature": False})
    # iat should be slightly in the past (clock skew buffer)
    assert payload["iat"] < int(time.time())


# ---------------------------------------------------------------------------
# get_installation_token
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_installation_token_returns_token(client: GitHubClient) -> None:
    respx.post(f"{_GITHUB_API}/app/installations/99/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_abc123", "expires_at": "2099-01-01T00:00:00Z"})
    )
    token = await client.get_installation_token(installation_id=99)
    assert token == "ghs_abc123"


@respx.mock
async def test_get_installation_token_raises_on_error(client: GitHubClient) -> None:
    respx.post(f"{_GITHUB_API}/app/installations/99/access_tokens").mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_installation_token(installation_id=99)


# ---------------------------------------------------------------------------
# patch_issue
# ---------------------------------------------------------------------------


@respx.mock
async def test_patch_issue_sends_labels_and_assignee(client: GitHubClient) -> None:
    route = respx.patch(f"{_GITHUB_API}/repos/owner/repo/issues/42").mock(return_value=httpx.Response(200, json={}))
    await client.patch_issue("tok", "owner", "repo", 42, ["bug", "P1"], "alice")
    assert route.called
    sent = route.calls[0].request
    import json

    body = json.loads(sent.content)
    assert body["labels"] == ["bug", "P1"]
    assert body["assignees"] == ["alice"]


@respx.mock
async def test_patch_issue_omits_assignees_when_none(client: GitHubClient) -> None:
    route = respx.patch(f"{_GITHUB_API}/repos/owner/repo/issues/42").mock(return_value=httpx.Response(200, json={}))
    await client.patch_issue("tok", "owner", "repo", 42, ["bug", "P1"], None)
    body = __import__("json").loads(route.calls[0].request.content)
    assert "assignees" not in body


@respx.mock
async def test_patch_issue_raises_on_4xx(client: GitHubClient) -> None:
    respx.patch(f"{_GITHUB_API}/repos/owner/repo/issues/42").mock(
        return_value=httpx.Response(422, json={"message": "Validation Failed"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.patch_issue("tok", "owner", "repo", 42, ["bug"], None)


# ---------------------------------------------------------------------------
# post_comment
# ---------------------------------------------------------------------------


@respx.mock
async def test_post_comment_sends_body(client: GitHubClient) -> None:
    route = respx.post(f"{_GITHUB_API}/repos/owner/repo/issues/42/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    await client.post_comment("tok", "owner", "repo", 42, "🤖 buma triage\n- bug")
    body = __import__("json").loads(route.calls[0].request.content)
    assert body["body"] == "🤖 buma triage\n- bug"


@respx.mock
async def test_post_comment_raises_on_error(client: GitHubClient) -> None:
    respx.post(f"{_GITHUB_API}/repos/owner/repo/issues/42/comments").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.post_comment("tok", "owner", "repo", 42, "comment")


# ---------------------------------------------------------------------------
# Authorization headers
# ---------------------------------------------------------------------------


def test_jwt_headers_contain_bearer(client: GitHubClient) -> None:
    headers = client._jwt_headers("my-jwt")
    assert headers["Authorization"] == "Bearer my-jwt"
    assert "application/vnd.github" in headers["Accept"]


def test_token_headers_contain_bearer(client: GitHubClient) -> None:
    headers = client._token_headers("ghs_tok")
    assert headers["Authorization"] == "Bearer ghs_tok"


# ---------------------------------------------------------------------------
# list_issues (pagination, T3 backfill)
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_issues_follows_pagination_and_yields_prs_unfiltered(client: GitHubClient) -> None:
    page2_url = f"{_GITHUB_API}/repositories/1/issues?state=all&per_page=100&page=2"
    first = respx.get(f"{_GITHUB_API}/repos/owner/repo/issues", params={"state": "all", "per_page": "100"}).mock(
        return_value=httpx.Response(
            200,
            json=[{"number": 1}, {"number": 2, "pull_request": {}}],
            headers={"Link": f'<{page2_url}>; rel="next", <{page2_url}>; rel="last"'},
        )
    )
    second = respx.get(page2_url).mock(return_value=httpx.Response(200, json=[{"number": 3}]))

    items = [item async for item in client.list_issues("tok", "owner", "repo")]

    assert [i["number"] for i in items] == [1, 2, 3]
    assert first.called and second.called
    assert first.calls[0].request.headers["Authorization"] == "Bearer tok"


@respx.mock
async def test_list_issues_raises_on_http_error(client: GitHubClient) -> None:
    respx.get(f"{_GITHUB_API}/repos/owner/repo/issues").mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        [item async for item in client.list_issues("tok", "owner", "repo")]
