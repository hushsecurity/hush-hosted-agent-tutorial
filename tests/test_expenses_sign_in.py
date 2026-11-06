"""Expenses' sign-in, end to end over HTTP, the way the gateway connects."""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull

from expenses import auth, server
from expenses.store import Store

pytestmark = pytest.mark.anyio

BASE_URL = "https://expenses.example.com"
REDIRECT_URI = "https://gateway.example.com/oauth/callback"


@pytest.fixture
async def expenses():
    store = Store()
    app = server.app(store, BASE_URL)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            yield http, store


async def sign_in(http: httpx2.AsyncClient, email: str, auth_method: str = "client_secret_post") -> str:
    """Signs `email` in the way the gateway does it, and returns the token."""
    metadata = (await http.get("/.well-known/oauth-authorization-server")).json()
    client = (await http.post(metadata["registration_endpoint"], json={
        "client_name": "gateway", "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": auth_method,
    })).json()

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    authorize = await http.get(metadata["authorization_endpoint"], params={
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT_URI,
        "state": "s1", "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": f"{BASE_URL}/mcp",
    })
    page = urlsplit(authorize.headers["location"])
    request_id = parse_qs(page.query)["request"][0]
    assert (await http.get(f"{page.path}?{page.query}")).status_code == 200

    signed_in = await http.post(page.path, data={"request": request_id, "email": email})
    back = urlsplit(signed_in.headers["location"])
    assert back.geturl().startswith(REDIRECT_URI)
    query = parse_qs(back.query)
    assert query["state"] == ["s1"]

    form = {"grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier}
    if auth_method == "client_secret_basic":
        # Only in the header, as many clients send it.
        token = await http.post(metadata["token_endpoint"], data=form,
                                auth=httpx2.BasicAuth(client["client_id"], client["client_secret"]))
    else:
        token = await http.post(metadata["token_endpoint"], data={
            **form, "client_id": client["client_id"], "client_secret": client["client_secret"]})
    assert token.status_code == 200, token.text
    return token.json()["access_token"]


async def test_a_client_may_send_its_secret_in_a_basic_header(expenses):
    http, _ = expenses
    assert await sign_in(http, "dana@example.com", auth_method="client_secret_basic")


async def test_mcp_needs_a_token(expenses):
    http, _ = expenses
    resp = await http.post("/mcp", json={})
    assert resp.status_code == 401
    assert "oauth-protected-resource" in resp.headers["www-authenticate"]


async def test_calls_arrive_as_the_signed_in_account(expenses):
    http, store = expenses
    token = await sign_in(http, "Dana@Example.com")

    http.headers["Authorization"] = f"Bearer {token}"
    async with streamable_http_client(f"{BASE_URL}/mcp", http_client=http) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("list_expenses", {})
            assert not result.is_error

    assert [(c.account, c.tool) for c in store.calls] == [("dana@example.com", "list_expenses")]


async def test_sign_in_wants_an_email(expenses):
    http, _ = expenses
    resp = await http.post("/signin", data={"request": "unknown", "email": "not-an-email"})
    assert resp.status_code == 400


async def test_an_expired_sign_in_link_says_so(expenses, monkeypatch):
    http, _ = expenses
    metadata = (await http.get("/.well-known/oauth-authorization-server")).json()
    client = (await http.post(metadata["registration_endpoint"], json={"redirect_uris": [REDIRECT_URI]})).json()
    authorize = await http.get(metadata["authorization_endpoint"], params={
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT_URI,
        "code_challenge": "x" * 43, "code_challenge_method": "S256",
    })
    page = urlsplit(authorize.headers["location"])

    monkeypatch.setattr(auth.time, "time", lambda: 2**40)
    resp = await http.get(f"{page.path}?{page.query}")
    assert resp.status_code == 400
    assert "expired" in resp.text


async def test_a_registered_client_is_known_after_a_restart_with_the_same_key():
    before = auth.Provider(BASE_URL, "key")
    client = OAuthClientInformationFull(client_id="x", client_secret="y", redirect_uris=[REDIRECT_URI],
                                        token_endpoint_auth_method="client_secret_post")
    await before.register_client(client)

    known = await auth.Provider(BASE_URL, "key").get_client(client.client_id)
    assert known is not None
    assert known.client_secret == client.client_secret
    assert [str(u) for u in known.redirect_uris] == [REDIRECT_URI]

    assert await auth.Provider(BASE_URL, "another key").get_client(client.client_id) is None
    registration, _, signature = client.client_id.partition(".")
    assert await before.get_client(f"{registration}x.{signature}") is None
    assert await before.get_client("not-a-client") is None
    assert await before.get_client("a.é") is None
