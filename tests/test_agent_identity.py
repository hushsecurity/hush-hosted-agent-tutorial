import asyncio
import dataclasses
import time
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from agent.identity import HushRefused, Identity, Session, claims
from tests.fake_hush import AGENT_RESOURCE, GATEWAY, SERVICE_ACCOUNT_TOKEN, FakeHush

pytestmark = pytest.mark.anyio

REDIRECT_URI = "http://localhost:8080/callback"


def identity_for(hush: FakeHush, tmp_path) -> Identity:
    token_file = tmp_path / "token"
    token_file.write_text(SERVICE_ACCOUNT_TOKEN + "\n")
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=hush.app()))
    return Identity(http, f"{GATEWAY}/mcp", REDIRECT_URI, token_file)


async def sign_in(identity: Identity, session: Session) -> dict:
    query = parse_qs(urlsplit(await identity.sign_in_url(session)).query)
    await identity.finish_sign_in(session, query["state"][0], "code-1")
    return query


async def test_the_user_signs_in_for_the_agent(tmp_path):
    hush = FakeHush()
    identity = identity_for(hush, tmp_path)

    session = Session()
    query = await sign_in(identity, session)

    assert query["resource"] == [AGENT_RESOURCE]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert session.user.claims["email"] == "dana@example.com"
    assert "code_verifier" in hush.requests[-1]


async def test_the_agent_asks_hush_what_users_sign_in_for(tmp_path):
    hush = FakeHush()
    identity = identity_for(hush, tmp_path)

    assert await identity.agent_resource() == AGENT_RESOURCE
    sent = hush.requests[-1]
    assert sent["grant_type"] == "client_credentials"
    assert sent["client_assertion"] == SERVICE_ACCOUNT_TOKEN


async def test_no_sign_in_for_an_agent_hush_does_not_know(tmp_path):
    identity = identity_for(FakeHush(registered=False), tmp_path)

    session = Session()
    with pytest.raises(HushRefused) as refused:
        await identity.sign_in_url(session)
    assert refused.value.step == "agent"
    assert "registered in Hush?" in refused.value.description
    assert not session.pending


async def test_no_guessed_resource_for_an_unexpected_issuer(tmp_path):
    identity = identity_for(FakeHush(), tmp_path)
    gateway = await identity.gateway()
    identity._gateway = dataclasses.replace(gateway, issuer="https://hush.example.com/agents")

    with pytest.raises(HushRefused) as refused:
        await identity.agent_resource()
    assert "issuer" in refused.value.description


async def test_the_exchange_names_both_the_user_and_the_agent(tmp_path):
    hush = FakeHush()
    identity = identity_for(hush, tmp_path)
    session = Session()
    await sign_in(identity, session)

    token = claims(await identity.exchange(session))

    assert token["email"] == "dana@example.com"
    assert token["act"] == {"sub": "agent-1"}
    sent = hush.requests[-1]
    assert sent["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
    assert sent["subject_token"] == session.user.access
    assert sent["client_assertion"] == SERVICE_ACCOUNT_TOKEN
    assert "client_secret" not in sent


async def test_a_user_the_agent_may_not_act_for_is_refused(tmp_path):
    identity = identity_for(FakeHush(user_email="sam@example.com"), tmp_path)
    session = Session()
    await sign_in(identity, session)

    with pytest.raises(HushRefused) as refused:
        await identity.exchange(session)
    assert refused.value.not_assigned


async def test_an_expiring_user_token_is_refreshed_before_the_exchange(tmp_path):
    hush = FakeHush()
    identity = identity_for(hush, tmp_path)
    session = Session()
    await sign_in(identity, session)
    session.user.expires_at = time.time() + 10

    await identity.exchange(session)

    assert [r["grant_type"] for r in hush.requests][-2] == "refresh_token"


async def test_the_exchange_needs_a_signed_in_user(tmp_path):
    with pytest.raises(HushRefused):
        await identity_for(FakeHush(), tmp_path).exchange(Session())


async def test_concurrent_requests_refresh_once(tmp_path):
    hush = FakeHush()
    identity = identity_for(hush, tmp_path)
    session = Session()
    await sign_in(identity, session)
    session.user.expires_at = time.time() + 10

    await asyncio.gather(identity.user_token(session), identity.user_token(session))

    assert [r["grant_type"] for r in hush.requests].count("refresh_token") == 1


async def test_a_sign_in_finishes_only_in_the_session_that_started_it(tmp_path):
    identity = identity_for(FakeHush(), tmp_path)
    query = parse_qs(urlsplit(await identity.sign_in_url(Session())).query)

    with pytest.raises(HushRefused):
        await identity.finish_sign_in(Session(), query["state"][0], "code-1")


async def test_a_network_error_while_refreshing_keeps_the_user_signed_in(tmp_path, monkeypatch):
    identity = identity_for(FakeHush(), tmp_path)
    session = Session()
    await sign_in(identity, session)
    session.user.expires_at = time.time() + 10

    async def unreachable(step, form):
        raise httpx2.ConnectError("unreachable")

    monkeypatch.setattr(identity, "_token", unreachable)
    with pytest.raises(httpx2.ConnectError):
        await identity.user_token(session)
    assert session.user is not None


async def test_a_refused_refresh_signs_the_user_out(tmp_path, monkeypatch):
    identity = identity_for(FakeHush(), tmp_path)
    session = Session()
    await sign_in(identity, session)
    session.user.expires_at = time.time() + 10

    async def refused(step, form):
        raise HushRefused(step, 400, "invalid_grant", "refresh token revoked")

    monkeypatch.setattr(identity, "_token", refused)
    with pytest.raises(HushRefused) as signed_out:
        await identity.user_token(session)
    assert session.user is None
    # Asked to sign in again at once, not shown Hush's refusal first.
    assert signed_out.value.error == "login_required"
    assert "sign in again" in signed_out.value.description


async def test_hush_failing_a_refresh_keeps_the_user_signed_in(tmp_path, monkeypatch):
    identity = identity_for(FakeHush(), tmp_path)
    session = Session()
    await sign_in(identity, session)
    session.user.expires_at = time.time() + 10

    async def failing(step, form):
        raise HushRefused(step, 503, "", "Service Unavailable")

    monkeypatch.setattr(identity, "_token", failing)
    with pytest.raises(HushRefused) as failed:
        await identity.user_token(session)
    assert failed.value.status == 503
    assert session.user is not None
