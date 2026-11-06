from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from agent import web
from tests.fake_hush import FakeHush
from tests.test_agent_identity import identity_for

pytestmark = pytest.mark.anyio

AGENT_URL = "http://localhost:8080"


async def signed_in_client(hush: FakeHush, tmp_path) -> httpx2.AsyncClient:
    app = web.build(identity_for(hush, tmp_path), AGENT_URL)
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://localhost:8080")
    sign_in = await client.get("/signin")
    state = parse_qs(urlsplit(sign_in.headers["location"]).query)["state"][0]
    assert (await client.get("/callback", params={"state": state, "code": "code-1"})).status_code in (302, 307)
    return client


async def test_signed_in_user_and_the_agents_token(tmp_path):
    client = await signed_in_client(FakeHush(), tmp_path)

    assert (await client.get("/api/me")).json()["email"] == "dana@example.com"
    exchanged = (await client.get("/api/exchange")).json()
    assert exchanged["claims"]["act"] == {"sub": "agent-1"}


async def test_refused_when_not_assigned(tmp_path):
    client = await signed_in_client(FakeHush(user_email="sam@example.com"), tmp_path)

    resp = await client.get("/api/exchange")
    assert resp.status_code == 403
    assert resp.json()["not_assigned"]


async def test_a_sign_in_hush_refuses_keeps_no_session(tmp_path):
    app = web.build(identity_for(FakeHush(registered=False), tmp_path), AGENT_URL)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=AGENT_URL) as client:
        resp = await client.get("/signin")
    assert resp.status_code == 401
    assert "registered in Hush?" in resp.json()["message"]
    assert "set-cookie" not in resp.headers


async def test_a_refused_sign_in_is_reported(tmp_path):
    app = web.build(identity_for(FakeHush(), tmp_path), AGENT_URL)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://localhost") as client:
        resp = await client.get("/callback", params={"error": "access_denied", "error_description": "nope"})
    assert resp.status_code == 403
    assert resp.json()["message"] == "nope"


async def test_another_browser_is_not_signed_in(tmp_path):
    client = await signed_in_client(FakeHush(), tmp_path)
    other = httpx2.AsyncClient(transport=client._transport, base_url="http://localhost:8080")

    assert (await other.get("/api/me")).json() == {"signed_in": False}
    assert (await other.get("/api/exchange")).status_code == 401


async def test_no_session_until_a_sign_in_starts(tmp_path):
    app = web.build(identity_for(FakeHush(), tmp_path), AGENT_URL)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=AGENT_URL) as client:
        for path in ("/", "/healthz", "/api/me"):
            assert "set-cookie" not in (await client.get(path)).headers, path
        assert "set-cookie" in (await client.get("/signin")).headers


async def test_the_cookie_is_secure_when_the_agent_is_on_https(tmp_path):
    app = web.build(identity_for(FakeHush(), tmp_path), "https://agent.example.com")
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="https://agent.example.com") as client:
        assert "secure" in (await client.get("/signin")).headers["set-cookie"].lower()


async def test_an_exchange_that_fails_is_reported(tmp_path):
    client = await signed_in_client(FakeHush(), tmp_path)
    (tmp_path / "token").unlink()

    resp = await client.get("/api/exchange")
    assert resp.status_code == 502
    assert resp.json()["ok"] is False


async def test_a_session_the_agent_no_longer_has_is_told_to_sign_in_again(tmp_path):
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=web.build(identity_for(FakeHush(), tmp_path), AGENT_URL)),
                                base_url=AGENT_URL, cookies={web.SESSION_COOKIE: "from-before-a-restart"})
    assert "sign in again" in (await client.get("/api/exchange")).json()["message"]

    # Signed in again, the session is like any other: losing the sign-in later
    # isn't a restart.
    sign_in = await client.get("/signin")
    state = parse_qs(urlsplit(sign_in.headers["location"]).query)["state"][0]
    await client.get("/callback", params={"state": state, "code": "code-1"})
    assert (await client.get("/api/me")).json()["signed_in"]
    await client.post("/api/signout")
    assert (await client.get("/api/exchange")).json()["message"] == "sign in first"

    fresh = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=web.build(identity_for(FakeHush(), tmp_path), AGENT_URL)), base_url=AGENT_URL)
    assert (await fresh.get("/api/exchange")).json()["message"] == "sign in first"
