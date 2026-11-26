"""Putting Expenses in Hush through its API, and taking it out."""

import asyncio

import httpx2
import pytest

import setup.__main__ as setup_main
from setup import hush
from setup.__main__ import Setup, answering, explain, status_app
from tests.fake_hush_api import API, KEY, FakeHushAPI

pytestmark = pytest.mark.anyio

EXPENSES = hush.Expenses("expenses-tutorial", "dep-1", "https://calm-river.trycloudflare.com")
SERVICE = "http://tutorial-expenses:8000"


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


async def test_a_second_install_in_the_org_needs_a_display_name_of_its_own():
    fake = FakeHushAPI()
    await hush.put(api_for(fake), EXPENSES)
    other = hush.Expenses("expenses-testing", "dep-2", EXPENSES.url)

    with pytest.raises(hush.NameTaken, match="expenses.displayName"):
        await hush.put(api_for(fake), other)
    other.display_name = "Expenses (testing)"
    app = await hush.put(api_for(fake), other)

    assert app["display_name"] == "Expenses (testing)"
    assert fake.custom["expenses-testing"]["display_name"] == "Expenses (testing)"


async def test_a_new_display_name_renames_the_app_on_the_gateway():
    fake = FakeHushAPI()
    first = await hush.put(api_for(fake), EXPENSES)

    renamed = hush.Expenses(EXPENSES.name, "dep-1", EXPENSES.url, display_name="Expenses (renamed)")
    again = await hush.put(api_for(fake), renamed)

    assert again["id"] == first["id"]
    assert fake.apps[first["id"]]["display_name"] == "Expenses (renamed)"


async def test_an_unchanged_display_name_isnt_sent_again():
    fake = FakeHushAPI()
    await hush.put(api_for(fake), EXPENSES)
    fake.patches = []

    await hush.put(api_for(fake), EXPENSES)

    assert fake.patches == [{"assign_all": True}]


async def test_the_status_names_expenses_before_it_is_in_hush(monkeypatch):
    monkeypatch.setenv("EXPENSES_DISPLAY_NAME", "Expenses (testing)")
    assert Setup(httpx2.AsyncClient()).status["display_name"] == "Expenses (testing)"


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


async def test_waits_for_expenses_to_answer_inside_the_cluster():
    answers = [httpx2.ConnectError("refused"), httpx2.Response(502), httpx2.Response(200, json={})]
    asked = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        asked.append(str(request.url))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
        await answering(http, SERVICE, interval=0)
    assert asked == [f"{SERVICE}/.well-known/oauth-protected-resource/mcp"] * 3


async def test_gives_up_on_expenses_that_never_answers():
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda r: httpx2.Response(502))) as http:
        with pytest.raises(RuntimeError, match="doesn't answer"):
            await answering(http, SERVICE, attempts=3, interval=0)


async def test_ready_only_once_expenses_is_in_hush():
    setup = Setup(FakeHushAPI().client())
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=status_app(setup)), base_url="http://setup") as c:
        assert (await c.get("/ready")).status_code == 503
        setup.status["state"] = "ready"
        assert (await c.get("/ready")).status_code == 200
        assert (await c.get("/status")).json()["state"] == "ready"


def agent_rule(op: str, value) -> dict:
    return {"all_of": [{"any_of": [{"source": "agent", "property": "id", "op": op, "value": value}]}]}


async def test_tells_whether_expenses_is_assigned_to_the_agent():
    fake = FakeHushAPI()
    setup = Setup(fake.client())
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=status_app(setup)), base_url="http://setup") as c:
        assert (await c.get("/assigned/agt-1")).json() == {"assigned": None, "error": None}

        setup.api, setup.app_id = api_for(fake), (await hush.put(api_for(fake), EXPENSES))["id"]
        assert (await c.get("/assigned/agt-1")).json()["assigned"] is False

        app = fake.apps[setup.app_id]
        app["assignments"] = [{"all_of": [{"any_of": [{"source": "user", "property": "email", "op": "eq",
                                                       "value": "agt-1"}]}]}, agent_rule("neq", "agt-1")]
        assert (await c.get("/assigned/agt-1")).json()["assigned"] is False
        app["assignments"].append(agent_rule("in", "agt-10"))
        assert (await c.get("/assigned/agt-1")).json()["assigned"] is False
        app["assignments"].append(agent_rule("in", ["agt-0", "agt-1"]))
        assert (await c.get("/assigned/agt-1")).json()["assigned"] is True
        app["assignments"] = [agent_rule("eq", "agt-1")]
        assert (await c.get("/assigned/agt-1")).json()["assigned"] is True


async def test_a_failed_assignment_check_is_reported_and_logged_once(caplog):
    fake = FakeHushAPI()
    setup = Setup(fake.client())
    setup.api, setup.app_id = api_for(fake), "app-gone"
    assert (await setup.assigned_to("agt-1"))["assigned"] is None
    assert "404" in (await setup.assigned_to("agt-1"))["error"]
    assert len([r for r in caplog.records if "assignments failed" in r.getMessage()]) == 1


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

    setup = Setup(FakeHushAPI().client())
    monkeypatch.setattr(setup_main, "expenses_in_hush", no_address)
    monkeypatch.setattr(setup_main, "RETRY_SECONDS", 0.01)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await setup.put()
    assert setup.status == {"state": "failed", "error": "the tunnel reported no address",
                            "display_name": hush.DISPLAY_NAME}


async def test_hush_learns_the_address_only_once_expenses_answers(monkeypatch):
    fake = FakeHushAPI()

    async def with_fake_hush(http, with_url):
        return api_for(fake), EXPENSES

    async def not_yet(http, url):
        raise RuntimeError(f"Expenses doesn't answer at {url}")

    setup = Setup(fake.client(), SERVICE)
    monkeypatch.setattr(setup_main, "expenses_in_hush", with_fake_hush)
    monkeypatch.setattr(setup_main, "answering", not_yet)
    monkeypatch.setattr(setup_main, "RETRY_SECONDS", 0.01)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await setup.put()
    assert fake.calls == []
    assert setup.status == {"state": "failed", "error": f"Expenses doesn't answer at {SERVICE}",
                            "display_name": hush.DISPLAY_NAME}


CLUSTER = "https://oidc.cluster.example.com/id/ABC"
ASSISTANT = "system:serviceaccount:tutorial:spend-assistant"


def ready_setup(fake: FakeHushAPI, app_id: str) -> Setup:
    setup = Setup(fake.client(), agent_subject=ASSISTANT)
    setup.status["state"] = "ready"
    setup.api, setup.expenses, setup.app_id = api_for(fake), EXPENSES, app_id
    return setup


AGENT_RULE = {"all_of": [{"any_of": [{"source": "agent", "property": "id", "op": "eq", "value": "agt-1"}]}]}


async def test_start_over_unregisters_the_agent_and_makes_the_app_anew():
    fake = FakeHushAPI()
    fake.add_agent("agt-1", (CLUSTER, ASSISTANT), name="spend-assistant")
    first = await hush.put(api_for(fake), EXPENSES)
    fake.apps[first["id"]]["assignments"] = [AGENT_RULE]
    setup = ready_setup(fake, first["id"])

    result = await setup.start_over(CLUSTER)

    assert result["agent"] == {"ok": True, "detail": "unregistered spend-assistant"} and result["app"]["ok"]
    [app] = fake.apps.values()
    assert (app["id"] != first["id"], app["assignments"], app["assign_all"]) == (True, [], True)
    assert setup.app_id == app["id"] and not fake.agents
    assert "expenses-tutorial" in fake.custom
    assert (await setup.start_over(CLUSTER))["agent"] == {"ok": True, "detail": "it wasn't registered"}


async def test_start_over_unregisters_only_the_tutorials_agent():
    fake = FakeHushAPI()
    fake.add_agent("agt-1", (CLUSTER, ASSISTANT))
    fake.add_agent("agt-2", (CLUSTER, "system:serviceaccount:tutorial:someone-else"))
    fake.add_agent("agt-3", ("https://oidc.other.example.com", ASSISTANT))
    fake.add_agent("agt-4", (CLUSTER, ASSISTANT), (CLUSTER, "system:serviceaccount:prod:billing"), name="billing")
    fake.add_agent("agt-5", (CLUSTER + "/", ASSISTANT))
    first = await hush.put(api_for(fake), EXPENSES)

    result = await ready_setup(fake, first["id"]).start_over(CLUSTER)

    assert sorted(fake.agents) == ["agt-2", "agt-3", "agt-4"]
    assert result["agent"]["ok"] is False and "left billing alone" in result["agent"]["detail"]
    assert result["app"]["ok"]


async def test_start_over_tells_of_an_agent_named_as_the_page_says_but_without_authentication():
    fake = FakeHushAPI()
    fake.add_agent("agt-1", name="spend-assistant")
    fake.add_agent("agt-2", name="half-done")
    first = await hush.put(api_for(fake), EXPENSES)

    result = await ready_setup(fake, first["id"]).start_over(CLUSTER)

    assert result["agent"] == {"ok": None, "detail": "found spend-assistant with no authentication, so it was "
                                                     "left in place. Remove it in the Hush UI if you'd like to "
                                                     "register it from scratch"}
    assert sorted(fake.agents) == ["agt-1", "agt-2"] and result["app"]["ok"]


async def test_start_over_unregisters_the_agent_and_tells_of_another_without_authentication():
    fake = FakeHushAPI()
    fake.add_agent("agt-1", (CLUSTER, ASSISTANT), name="assistant")
    fake.add_agent("agt-2", name="spend-assistant")
    first = await hush.put(api_for(fake), EXPENSES)
    result = await ready_setup(fake, first["id"]).start_over(CLUSTER)
    assert result["agent"]["ok"] is None
    assert result["agent"]["detail"].startswith("unregistered assistant; found spend-assistant")
    assert list(fake.agents) == ["agt-2"]


@pytest.mark.parametrize("issuer, subject", [(None, ASSISTANT), (CLUSTER, None)])
async def test_start_over_without_knowing_the_agent_unregisters_nothing(issuer, subject):
    fake = FakeHushAPI()
    fake.add_agent("agt-1", (CLUSTER, ASSISTANT))
    first = await hush.put(api_for(fake), EXPENSES)
    setup = ready_setup(fake, first["id"])
    setup.agent_subject = subject

    result = await setup.start_over(issuer)

    assert result["agent"] == {"ok": False, "detail": "couldn't tell the agent's Kubernetes identity"}
    assert result["app"]["ok"] and "agt-1" in fake.agents


async def test_start_over_without_a_registered_agent_still_makes_the_app_anew():
    fake = FakeHushAPI()
    first = await hush.put(api_for(fake), EXPENSES)
    result = await ready_setup(fake, first["id"]).start_over(CLUSTER)
    assert result["agent"] == {"ok": True, "detail": "it wasn't registered"} and result["app"]["ok"]


async def test_start_over_goes_on_when_unregistering_fails():
    fake = FakeHushAPI()
    fake.add_agent("agt-1", (CLUSTER, ASSISTANT))
    fake.broken_agents.add("agt-1")
    first = await hush.put(api_for(fake), EXPENSES)
    result = await ready_setup(fake, first["id"]).start_over(CLUSTER)
    assert not result["agent"]["ok"] and "500" in result["agent"]["detail"]
    assert result["app"]["ok"] and first["id"] not in fake.apps


async def test_start_over_leaves_an_app_the_tutorial_did_not_make():
    fake = FakeHushAPI()
    first = await hush.put(api_for(fake), EXPENSES)
    fake.custom["expenses-tutorial"]["description"] = "Someone's own"
    setup = ready_setup(fake, first["id"])
    result = await setup.start_over(CLUSTER)
    assert not result["app"]["ok"] and first["id"] in fake.apps
    assert (setup.app_id, setup.status["state"], setup.putting_back) == (first["id"], "ready", None)


async def test_a_half_made_app_is_put_back(monkeypatch):
    monkeypatch.setattr(setup_main, "RETRY_SECONDS", 0)
    fake = FakeHushAPI()
    failing = {"left": 0}
    real_handle = fake.handle

    def handle(request):
        if request.method == "POST" and request.url.path.startswith("/v1/applications/mcp/") and failing["left"]:
            failing["left"] -= 1
            return httpx2.Response(503, json={"detail": "trouble"})
        return real_handle(request)
    fake.handle = handle
    first = await hush.put(api_for(fake), EXPENSES)
    setup = ready_setup(fake, first["id"])
    failing["left"] = 1

    result = await setup.start_over(CLUSTER)

    assert not result["app"]["ok"] and "once Expenses is back" in result["app"]["detail"]
    assert setup.status["state"] == "failed" and setup.app_id is None and not fake.apps
    await setup.putting_back
    [app] = fake.apps.values()
    assert (setup.status["state"], setup.app_id) == ("ready", app["id"])


async def test_start_over_waits_until_expenses_is_in_hush():
    result = await Setup(FakeHushAPI().client(), agent_subject=ASSISTANT).start_over(CLUSTER)
    assert not result["agent"]["ok"] and not result["app"]["ok"]

