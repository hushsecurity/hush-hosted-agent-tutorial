import datetime
import time

import httpx2
import pytest

from starlette.applications import Starlette
from starlette.routing import Mount

from agent import web
from agent.gateway import ExpensesThroughGateway, Outcome
from agent.tutorial import ExpensesSide, pair_with_log
from expenses import internal
from expenses.store import Store
from setup import hush as hush_api
from setup.__main__ import Setup, status_app
from tests.fake_hush import AGENT_ID, AGENT_NAME, CLUSTER_ISSUER, GATEWAY, ISSUER, SERVICE_ACCOUNT, FakeHush
from tests.fake_hush_api import API, KEY, FakeHushAPI
from tests.test_agent_identity import identity_for
from tests.test_agent_web import AGENT_URL, sign_in_with

pytestmark = pytest.mark.anyio

EXPENSES_URL = "https://expenses.example.com"


IN_HUSH = {"state": "ready", "error": None, "app": "custom-expenses-tutorial", "display_name": "Expenses (tutorial)"}


def setup_with(assignments: list) -> Setup:
    """Setup once Expenses is in Hush, with these assignment rules."""
    fake = FakeHushAPI()
    setup = Setup(fake.client())
    setup.status = dict(IN_HUSH)
    setup.api = hush_api.HushAPI(fake.client(), API, *KEY)
    setup.app_id = fake.add_app("custom-expenses-tutorial", "dep-1", assignments=assignments)["id"]
    return setup


AGENT_RULE = {"all_of": [{"any_of": [{"source": "agent", "property": "id", "op": "eq", "value": AGENT_ID}]}]}


def page_for(hush: FakeHush, tmp_path, store: Store, internal_url: str | None = "http://cluster/expenses",
             setup_url: str | None = "http://cluster/setup", assignments: list | None = None,
             setup: Setup | None = None):
    """The agent's page, with Expenses' internal port and setup's status
    behind one address."""
    identity = identity_for(hush, tmp_path)
    cluster = Starlette(routes=[Mount("/expenses", internal.build(store, EXPENSES_URL, "code-1")),
                                Mount("/setup", status_app(setup or setup_with(assignments or [])))])
    side = ExpensesSide(httpx2.AsyncClient(transport=httpx2.ASGITransport(app=cluster)), internal_url, setup_url)
    app = web.build(identity, ExpensesThroughGateway(identity, "custom-expenses-tutorial"), AGENT_URL, side,
                    release="tutorial", chart_version="0.1.0")
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=AGENT_URL)


async def test_the_page_is_served(tmp_path):
    async with page_for(FakeHush(), tmp_path, Store()) as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "Hosted Agent Tutorial" in resp.text


async def test_setup_shows_what_to_enter_in_hush(tmp_path):
    async with page_for(FakeHush(), tmp_path, Store()) as client:
        setup = (await client.get("/api/setup")).json()

    assert setup["agent"] == {"issuer": CLUSTER_ISSUER, "subject": SERVICE_ACCOUNT, "audience": [ISSUER],
                              "hush": {"id": AGENT_ID, "name": AGENT_NAME}}
    assert setup["gateway"] == {"url": f"{GATEWAY}/mcp", "issuer": ISSUER, "error": None}
    assert setup["expenses"] == {"app": "custom-expenses-tutorial", "url": EXPENSES_URL, "access_code": "code-1",
                                 "in_hush": IN_HUSH, "assigned": {"assigned": False, "error": None}}
    assert (setup["release"], setup["chart_version"]) == ("tutorial", "0.1.0")


async def test_setup_tells_once_expenses_is_assigned_to_the_agent(tmp_path):
    async with page_for(FakeHush(), tmp_path, Store(), assignments=[AGENT_RULE]) as client:
        setup = (await client.get("/api/setup")).json()
    assert setup["expenses"]["assigned"] == {"assigned": True, "error": None}


async def test_setup_without_expenses_or_a_token(tmp_path):
    async with page_for(FakeHush(), tmp_path, Store(), internal_url=None, setup_url=None) as client:
        (tmp_path / "token").unlink()
        setup = (await client.get("/api/setup")).json()

    assert list(setup["agent"]) == ["hush"]
    assert "error" in setup["agent"]["hush"]
    assert setup["expenses"]["url"] is None
    assert setup["expenses"]["in_hush"] == {"state": "unknown"}
    assert setup["expenses"]["assigned"] == {"assigned": None, "error": None}


async def test_setup_before_the_agent_is_registered(tmp_path):
    async with page_for(FakeHush(registered=False), tmp_path, Store()) as client:
        setup = (await client.get("/api/setup")).json()

    assert setup["agent"]["hush"]["unknown"] is True
    assert setup["agent"]["hush"]["reason"] == "invalid assertion"
    assert setup["expenses"]["assigned"] == {"assigned": None, "error": None}


async def test_a_call_shows_the_account_the_expenses_service_acted_on(tmp_path, monkeypatch):
    store = Store()

    async def run(self, session, tool, arguments):
        started = time.time()
        store.record("work@other.example.com", tool, arguments, "3 expenses")
        session.activity.events.append({"id": 1, "kind": "call", "tool": tool, "outcome": "done",
                                        "started": iso(started), "time": iso(time.time())})
        return Outcome("done", data=[])

    monkeypatch.setattr(ExpensesThroughGateway, "run", run)
    store.record("sam@example.com", "delete_expense", {"expense_id": 1}, "deleted")
    async with page_for(FakeHush(), tmp_path, store) as client:
        await sign_in_with(client)
        await client.post("/api/expenses/list")
        [event] = (await client.get("/api/activity")).json()["events"]

    assert event["seen"] == {"account": "work@other.example.com"}


def iso(seconds: float) -> str:
    return datetime.datetime.fromtimestamp(seconds, datetime.UTC).isoformat()


def at(second: int) -> str:
    return f"2026-11-17T12:00:{second:02d}+00:00"


def paired(events: list[dict], log: list[dict]) -> list:
    pair_with_log(events, log)
    return [e.get("seen", {}).get("account") for e in events]


def test_a_call_pairs_with_the_log_by_tool_and_time():
    events = [{"tool": "list_expenses", "started": at(10), "time": at(11)},
              {"tool": "submit_expense", "started": at(30), "time": at(31)}]
    # Newest first, as the Expenses service keeps it.
    log = [{"tool": "submit_expense", "time": at(50), "account": "dana@example.com"},
           {"tool": "list_expenses", "time": at(11), "account": "dana@example.com"}]
    assert paired(events, log) == ["dana@example.com", None]


def test_quick_calls_on_one_account_each_show_it():
    events = [{"tool": "list_expenses", "started": at(10), "time": at(11)},
              {"tool": "list_expenses", "started": at(13), "time": at(14)}]
    log = [{"tool": "list_expenses", "time": at(13), "account": "dana@example.com"},
           {"tool": "list_expenses", "time": at(11), "account": "dana@example.com"}]
    assert paired(events, log) == ["dana@example.com", "dana@example.com"]


def test_another_users_call_at_the_same_time_hides_the_account():
    events = [{"tool": "list_expenses", "started": at(10), "time": at(11)}]
    log = [{"tool": "list_expenses", "time": at(11), "account": "sam@example.com"},
           {"tool": "list_expenses", "time": at(11), "account": "dana@example.com"}]
    assert paired(events, log) == [None]


def setup_in_use(fake: FakeHushAPI) -> Setup:
    """Setup once the reader has done the steps: the agent registered and assigned."""
    fake.add_agent(AGENT_ID, (CLUSTER_ISSUER, SERVICE_ACCOUNT))
    setup = Setup(fake.client(), agent_subject=SERVICE_ACCOUNT)
    setup.status = dict(IN_HUSH)
    setup.api = hush_api.HushAPI(fake.client(), API, *KEY)
    setup.expenses = hush_api.Expenses("expenses-tutorial", "dep-1", EXPENSES_URL)
    setup.app_id = fake.add_app("custom-expenses-tutorial", "dep-1", assignments=[AGENT_RULE])["id"]
    return setup


async def test_start_over_puts_hush_and_expenses_back_and_signs_everyone_out(tmp_path):
    store, fake = Store(), FakeHushAPI()
    store.record("dana@example.com", "list_expenses", {}, "3 expenses")
    hush = FakeHush(registered=lambda: AGENT_ID in fake.agents)
    async with page_for(hush, tmp_path, store, setup=setup_in_use(fake)) as client:
        await sign_in_with(client)
        steps = (await client.post("/api/start-over", json={})).json()["steps"]
        signed_in = (await client.get("/api/me")).json()["signed_in"]

    assert all(step["ok"] for step in steps.values()), steps
    assert not signed_in
    assert AGENT_ID not in fake.agents
    assert [app["assignments"] for app in fake.apps.values()] == [[]]
    assert not store.calls


async def test_start_over_tells_of_an_agent_still_registered(tmp_path):
    fake = FakeHushAPI()
    setup = setup_in_use(fake)
    # Registered for the whole namespace: setup doesn't take it for the tutorial's own.
    fake.agents[AGENT_ID]["credentials"][0].update(subject="system:serviceaccount:tutorial:", subject_match="prefix")
    async with page_for(FakeHush(), tmp_path, Store(), setup=setup) as client:
        steps = (await client.post("/api/start-over", json={})).json()["steps"]
    assert steps["agent"] == {"ok": False, "detail": f"still registered as {AGENT_NAME}: remove it in the Hush UI"}
    assert AGENT_ID in fake.agents and steps["app"]["ok"]


@pytest.mark.parametrize("headers, json_body", [
    ({}, False),
    ({"origin": "https://elsewhere.example.com"}, True),
    ({"host": "rebound.example.com:8080", "origin": "http://rebound.example.com:8080"}, True),
])
async def test_start_over_answers_only_the_page(tmp_path, headers, json_body):
    store, fake = Store(), FakeHushAPI()
    store.record("dana@example.com", "list_expenses", {}, "3 expenses")
    async with page_for(FakeHush(), tmp_path, store, setup=setup_in_use(fake)) as client:
        sent = {"json": {}} if json_body else {"content": "x", "headers": {"content-type": "text/plain", **headers}}
        if json_body:
            sent["headers"] = headers
        resp = await client.post("/api/start-over", **sent)
    assert resp.status_code == 403
    assert AGENT_ID in fake.agents and store.calls


async def test_start_over_answers_the_page_by_its_loopback_address(tmp_path):
    async with page_for(FakeHush(), tmp_path, Store(), setup=setup_in_use(FakeHushAPI())) as client:
        resp = await client.post("/api/start-over", json={},
                                 headers={"host": "127.0.0.1:8080", "origin": "http://127.0.0.1:8080"})
    assert resp.status_code == 200



async def test_start_over_answers_the_page_whatever_the_case_of_its_address(tmp_path):
    identity = identity_for(FakeHush(), tmp_path)
    app = web.build(identity, ExpensesThroughGateway(identity, "custom-expenses-tutorial"), "http://LocalHost:8080",
                    ExpensesSide(httpx2.AsyncClient(), None, None))
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://localhost:8080") as client:
        resp = await client.post("/api/start-over", json={}, headers={"origin": "http://LOCALHOST:8080"})
    assert resp.status_code == 200


async def test_start_over_leaves_an_agent_without_authentication_with_a_note(tmp_path):
    fake = FakeHushAPI()
    setup = setup_in_use(fake)
    fake.agents[AGENT_ID].update(credentials=[], name=AGENT_NAME)
    async with page_for(FakeHush(registered=False), tmp_path, Store(), setup=setup) as client:
        steps = (await client.post("/api/start-over", json={})).json()["steps"]
    assert steps["agent"]["ok"] is None and steps["agent"]["detail"].startswith(f"found {AGENT_NAME} with no")
    assert AGENT_ID in fake.agents
