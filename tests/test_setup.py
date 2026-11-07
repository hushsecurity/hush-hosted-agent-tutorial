"""Putting Expenses in Hush through its API, and taking it out."""

import asyncio

import httpx2
import pytest

import setup.__main__ as setup_main
from setup import hush
from setup.__main__ import explain, status_app
from tests.fake_hush_api import API, KEY, FakeHushAPI

pytestmark = pytest.mark.anyio

EXPENSES = hush.Expenses("expenses-tutorial", "dep-1", "https://calm-river.trycloudflare.com")


def api_for(fake: FakeHushAPI) -> hush.HushAPI:
    return hush.HushAPI(fake.client(), API, *KEY)


async def test_puts_expenses_in_the_catalog_and_on_the_gateway_for_all_users():
    fake = FakeHushAPI()

    app = await hush.put(api_for(fake), EXPENSES)

    custom = fake.custom["expenses-tutorial"]
    assert custom["urls"] == [{"label": None, "url": "https://calm-river.trycloudflare.com/mcp"}]
    assert {t["name"]: t["type"] for t in custom["tools"]} == {
        "list_expenses": "read", "submit_expense": "write", "delete_expense": "destructive"}
    assert "auth" not in custom
    assert app["app_catalog_id"] == "custom-expenses-tutorial"
    assert app["deployment_ids"] == ["dep-1"]
    assert app["assign_all"] is True


async def test_a_new_address_updates_expenses_and_keeps_its_assignments():
    fake = FakeHushAPI()
    api = api_for(fake)
    first = await hush.put(api, EXPENSES)
    fake.apps[first["id"]]["assignments"] = [{"all_of": "the reader's agent"}]

    again = await hush.put(api, hush.Expenses(EXPENSES.name, "dep-1", "https://new-name.trycloudflare.com"))

    assert again["id"] == first["id"]
    assert len(fake.apps) == 1
    assert again["assignments"] == [{"all_of": "the reader's agent"}]
    assert fake.custom["expenses-tutorial"]["urls"][0]["url"] == "https://new-name.trycloudflare.com/mcp"


async def test_finds_its_application_past_the_first_page_and_on_its_gateway_only():
    fake = FakeHushAPI(page_size=1)
    fake.add_app("github", "dep-1")
    fake.add_app("custom-expenses-tutorial", "dep-other")
    await hush.put(api_for(fake), EXPENSES)

    await hush.put(api_for(fake), EXPENSES)

    ours = [a for a in fake.apps.values() if a["deployment_ids"] == ["dep-1"] and a["name"] != "github"]
    assert len(ours) == 1


async def test_remove_takes_it_off_the_gateway_then_out_of_the_catalog():
    fake = FakeHushAPI()
    api = api_for(fake)
    await hush.put(api, EXPENSES)

    await hush.remove(api, EXPENSES)

    assert fake.apps == {} and fake.custom == {}
    assert fake.calls[-2:] == ["DELETE /v1/applications/app-1", "DELETE /v1/applications/custom/expenses-tutorial"]


async def test_remove_when_hush_has_nothing():
    fake = FakeHushAPI()
    await hush.remove(api_for(fake), EXPENSES)
    assert fake.calls == ["GET /v1/applications/custom/expenses-tutorial/mcp"]


async def test_signs_in_again_when_its_token_expires():
    fake = FakeHushAPI()
    api = api_for(fake)
    await hush.put(api, EXPENSES)
    fake.token = "expired"

    await hush.put(api, EXPENSES)

    assert api._token == fake.token != "expired"


async def test_a_refused_key_is_reported():
    with pytest.raises(hush.HushAPIError) as refused:
        await hush.put(hush.HushAPI(FakeHushAPI().client(), API, "key-id", "wrong"), EXPENSES)
    assert refused.value.status == 401


def test_the_api_and_the_gateway_come_from_its_issuer():
    assert hush.api_and_deployment("https://api.example.com/v1/deployments/dep-1/agents") == (
        "https://api.example.com", "dep-1")
    with pytest.raises(ValueError):
        hush.api_and_deployment("https://hush.example.com/agents")


async def test_ready_only_once_expenses_is_in_hush():
    status = {"state": "working", "error": None}
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=status_app(status)), base_url="http://setup") as c:
        assert (await c.get("/ready")).status_code == 503
        status["state"] = "ready"
        assert (await c.get("/ready")).status_code == 200
        assert (await c.get("/status")).json()["state"] == "ready"


async def test_an_app_the_tutorial_did_not_make_is_left_alone():
    fake = FakeHushAPI()
    fake.custom["expenses-tutorial"] = {"name": "expenses-tutorial", "description": "Someone's own"}
    fake.add_app("custom-expenses-tutorial", "dep-other")
    api = api_for(fake)

    with pytest.raises(hush.NotOurs) as put:
        await hush.put(api, EXPENSES)
    with pytest.raises(hush.NotOurs):
        await hush.remove(api, EXPENSES)

    assert "set expenses.name" in str(put.value)
    assert fake.custom["expenses-tutorial"] == {"name": "expenses-tutorial", "description": "Someone's own"}
    assert len(fake.apps) == 1
    assert not [c for c in fake.calls if not c.startswith("GET")]


async def test_an_unmarked_app_on_its_gateway_fails_the_removal():
    fake = FakeHushAPI()
    api = api_for(fake)
    await hush.put(api, EXPENSES)
    fake.custom["expenses-tutorial"]["description"] = "edited in the UI"

    with pytest.raises(hush.Unmarked):
        await hush.remove(api, EXPENSES)
    assert len(fake.apps) == 1 and "expenses-tutorial" in fake.custom


async def test_its_own_app_is_marked():
    fake = FakeHushAPI()
    await hush.put(api_for(fake), EXPENSES)
    assert fake.custom["expenses-tutorial"]["description"] == hush.MARK


def test_a_missing_scope_is_named():
    assert "applications:write" in explain(hush.HushAPIError("GET", "/v1/applications", 403, "forbidden"))


async def test_a_failed_removal_fails_the_uninstall(monkeypatch):
    async def with_a_refused_key(http, with_url):
        return hush.HushAPI(FakeHushAPI().client(), API, "key-id", "expired"), EXPENSES

    monkeypatch.setattr(setup_main, "expenses_in_hush", with_a_refused_key)
    monkeypatch.setenv("HELM_RELEASE", "tutorial")
    monkeypatch.setenv("HELM_NAMESPACE", "demo")
    with pytest.raises(SystemExit) as exited:
        await setup_main.remove()
    assert exited.value.code == 1


async def test_a_tunnel_without_an_address_is_a_failed_status(monkeypatch):
    async def no_address(http, with_url):
        raise RuntimeError("the tunnel reported no address")

    status = {"state": "working", "error": None}
    monkeypatch.setattr(setup_main, "expenses_in_hush", no_address)
    monkeypatch.setattr(setup_main, "RETRY_SECONDS", 0.01)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await setup_main.put(status)
    assert status == {"state": "failed", "error": "the tunnel reported no address"}
