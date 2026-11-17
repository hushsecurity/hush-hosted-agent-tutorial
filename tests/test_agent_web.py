import logging
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from uvicorn.logging import AccessFormatter

from agent import web
from agent.gateway import ExpensesThroughGateway, Outcome
from tests.fake_hush import FakeHush
from tests.test_agent_identity import identity_for

pytestmark = pytest.mark.anyio

AGENT_URL = "http://localhost:8080"


def app_for(hush: FakeHush, tmp_path, agent_url: str = AGENT_URL):
    identity = identity_for(hush, tmp_path)
    return web.build(identity, ExpensesThroughGateway(identity, "custom-expenses"), agent_url)


async def sign_in_with(client: httpx2.AsyncClient) -> None:
    sign_in = await client.get("/signin")
    state = parse_qs(urlsplit(sign_in.headers["location"]).query)["state"][0]
    assert (await client.get("/callback", params={"state": state, "code": "code-1"})).status_code in (302, 307)


async def signed_in_client(hush: FakeHush, tmp_path) -> httpx2.AsyncClient:
    app = app_for(hush, tmp_path)
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=AGENT_URL)
    await sign_in_with(client)
    return client


async def test_signed_in_user(tmp_path):
    client = await signed_in_client(FakeHush(), tmp_path)
    assert (await client.get("/api/me")).json()["email"] == "dana@example.com"


async def test_refused_when_not_assigned(tmp_path):
    client = await signed_in_client(FakeHush(user_email="sam@example.com"), tmp_path)
    assert (await client.post("/api/expenses/list")).json()["kind"] == "not_assigned"


async def test_the_activity_is_this_users_newest_first_and_ends_with_the_sign_in(tmp_path, monkeypatch):
    async def run(self, session, tool, arguments):
        session.activity.events.append({"tool": tool})
        return Outcome("done", data=[])

    monkeypatch.setattr(ExpensesThroughGateway, "run", run)
    client = await signed_in_client(FakeHush(), tmp_path)
    await client.post("/api/expenses/list")
    await client.post("/api/expenses/delete", content='{"expense_id": 1}')
    assert [e["tool"] for e in (await client.get("/api/activity")).json()["events"]] == ["delete_expense", "list_expenses"]

    other = httpx2.AsyncClient(transport=client._transport, base_url=AGENT_URL)
    assert (await other.get("/api/activity")).json() == {"events": []}

    await client.post("/api/signout")
    assert (await client.get("/api/activity")).json() == {"events": []}


async def test_a_sign_in_hush_refuses_keeps_no_session(tmp_path):
    app = app_for(FakeHush(registered=False), tmp_path)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=AGENT_URL) as client:
        resp = await client.get("/signin")
    assert resp.status_code == 307
    assert parse_qs(urlsplit(resp.headers["location"]).query)["signin_error"][0].startswith(
        "Hush didn't accept the agent's identity")
    assert "set-cookie" not in resp.headers


async def test_a_refused_sign_in_is_reported(tmp_path):
    app = app_for(FakeHush(), tmp_path)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=AGENT_URL) as client:
        resp = await client.get("/callback", params={"error": "access_denied", "error_description": "nope"})
    assert resp.status_code == 307
    assert resp.headers["location"] == "/?signin_error=nope"


async def test_another_browser_is_not_signed_in(tmp_path):
    client = await signed_in_client(FakeHush(), tmp_path)
    other = httpx2.AsyncClient(transport=client._transport, base_url=AGENT_URL)

    assert (await other.get("/api/me")).json() == {"signed_in": False}
    assert (await other.post("/api/expenses/list")).json()["kind"] == "signed_out"


async def test_no_session_until_a_sign_in_starts(tmp_path):
    app = app_for(FakeHush(), tmp_path)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=AGENT_URL) as client:
        for path in ("/", "/healthz", "/api/me"):
            assert "set-cookie" not in (await client.get(path)).headers, path
        assert "set-cookie" in (await client.get("/signin")).headers


async def test_the_cookie_is_secure_when_the_agent_is_on_https(tmp_path):
    app = app_for(FakeHush(), tmp_path, "https://agent.example.com")
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="https://agent.example.com") as client:
        assert "secure" in (await client.get("/signin")).headers["set-cookie"].lower()


async def test_an_exchange_that_fails_is_reported(tmp_path):
    client = await signed_in_client(FakeHush(), tmp_path)
    (tmp_path / "token").unlink()

    assert (await client.post("/api/expenses/list")).json()["kind"] == "error"
    [event] = (await client.get("/api/activity")).json()["events"]
    assert "token" in event["exchange"]["error"]


async def test_a_consent_check_that_fails_is_reported(tmp_path):
    client = await signed_in_client(FakeHush(), tmp_path)
    (tmp_path / "token").unlink()

    resp = await client.get("/api/consent/req-1")
    assert resp.status_code == 502


@pytest.mark.parametrize("body", ['{"amount": "lots"}', '{"amount": "inf"}', '{"amount": -3}', "[]", "not json"])
async def test_bad_submit_input_is_a_400(tmp_path, body):
    client = await signed_in_client(FakeHush(), tmp_path)
    resp = await client.post("/api/expenses/submit", content=body)
    assert resp.status_code == 400


@pytest.mark.parametrize("expense_id", ["1001.9", '"abc"', '"²"', "true", "null"])
async def test_a_delete_needs_a_whole_number_id(tmp_path, expense_id):
    client = await signed_in_client(FakeHush(), tmp_path)
    resp = await client.post("/api/expenses/delete", content=f'{{"expense_id": {expense_id}}}')
    assert resp.status_code == 400


async def test_a_whole_number_id_is_accepted(tmp_path, monkeypatch):
    sent = []

    async def run(self, session, tool, arguments):
        sent.append(arguments)
        return Outcome("done")

    monkeypatch.setattr(ExpensesThroughGateway, "run", run)
    client = await signed_in_client(FakeHush(), tmp_path)
    for expense_id in ("1001", '"1002"'):
        assert (await client.post("/api/expenses/delete", content=f'{{"expense_id": {expense_id}}}')).status_code == 200
    assert sent == [{"expense_id": 1001}, {"expense_id": 1002}]


async def test_a_session_the_agent_no_longer_has_is_told_to_sign_in_again(tmp_path):
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app_for(FakeHush(), tmp_path)),
                                base_url=AGENT_URL, cookies={web.SESSION_COOKIE: "from-before-a-restart"})
    assert "sign in again" in (await client.post("/api/expenses/list")).json()["message"]

    # Signed in again, the session is like any other: losing the sign-in later
    # isn't a restart.
    sign_in = await client.get("/signin")
    state = parse_qs(urlsplit(sign_in.headers["location"]).query)["state"][0]
    await client.get("/callback", params={"state": state, "code": "code-1"})
    assert (await client.get("/api/me")).json()["signed_in"]
    await client.post("/api/signout")
    assert (await client.post("/api/expenses/list")).json()["message"] == "sign in first"

    fresh = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app_for(FakeHush(), tmp_path)), base_url=AGENT_URL)
    assert (await fresh.post("/api/expenses/list")).json()["message"] == "sign in first"


NO_OUTCOME = {"message": "", "data": None, "url": None, "request_id": None}


async def test_a_session_the_agent_no_longer_has_reads_as_signed_out(tmp_path):
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app_for(FakeHush(), tmp_path)),
                                base_url=AGENT_URL, cookies={web.SESSION_COOKIE: "from-before-a-restart"})
    listed = (await client.post("/api/expenses/list")).json()
    assert listed["kind"] == "signed_out"
    assert "sign in again" in listed["message"]


@pytest.mark.parametrize("data, expected", [
    ([{"id": 1}], [{"id": 1}]),
    ({"id": 1}, [{"id": 1}]),
    ([], []),
])
async def test_expenses_listed_are_a_list(tmp_path, monkeypatch, data, expected):
    async def run(self, session, tool, arguments):
        return Outcome("done", data=data)

    monkeypatch.setattr(ExpensesThroughGateway, "run", run)
    client = await signed_in_client(FakeHush(), tmp_path)
    assert (await client.post("/api/expenses/list")).json() == {**NO_OUTCOME, "kind": "done", "data": expected}


async def test_a_list_answer_that_isnt_a_list_is_an_error(tmp_path, monkeypatch):
    async def run(self, session, tool, arguments):
        return Outcome("done", data="Expenses is down for maintenance")

    monkeypatch.setattr(ExpensesThroughGateway, "run", run)
    client = await signed_in_client(FakeHush(), tmp_path)
    listed = (await client.post("/api/expenses/list")).json()
    assert listed["kind"] == "error"
    assert "Expenses is down for maintenance" in listed["message"]


def test_the_access_log_leaves_out_the_sign_in_code():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:5000", "GET", "/callback?code=code-1&state=s", "1.1", 307), None)
    assert web.WithoutQueries().filter(record)
    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False).format(record)
    assert line == '127.0.0.1:5000 - "GET /callback HTTP/1.1" 307 Temporary Redirect'
