import httpx2
import pytest

from starlette.applications import Starlette
from starlette.routing import Mount

from agent import web
from agent.gateway import ExpensesThroughGateway
from agent.tutorial import ExpensesSide
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
             setup_url: str | None = "http://cluster/setup", assignments: list | None = None):
    """The agent's page, with Expenses' internal port and setup's status
    behind one address."""
    identity = identity_for(hush, tmp_path)
    cluster = Starlette(routes=[Mount("/expenses", internal.build(store, EXPENSES_URL, "code-1")),
                                Mount("/setup", status_app(setup_with(assignments or [])))])
    side = ExpensesSide(httpx2.AsyncClient(transport=httpx2.ASGITransport(app=cluster)), internal_url, setup_url)
    app = web.build(identity, ExpensesThroughGateway(identity, "custom-expenses-tutorial"), AGENT_URL, side,
                    release="tutorial")
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
    assert setup["release"] == "tutorial"


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


async def test_each_user_sees_only_their_own_calls(tmp_path):
    store = Store()
    store.record("dana@example.com", "list_expenses", {}, "3 expenses")
    store.record("sam@example.com", "list_expenses", {}, "3 expenses")

    async with page_for(FakeHush(), tmp_path, store) as client:
        assert (await client.get("/api/calls")).json() == {"account": None, "calls": []}
        await sign_in_with(client)
        shown = (await client.get("/api/calls")).json()

    assert shown["account"] == "dana@example.com"
    assert [c["account"] for c in shown["calls"]] == ["dana@example.com"]
