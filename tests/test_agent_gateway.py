import contextlib
import time

import httpx2
import pytest

from agent.gateway import ExpensesThroughGateway, Outcome, _record_call
from agent.identity import Activity, HushRefused, Identity, Session
from tests.fake_gateway import APP, CONNECT_URL, CONSENT_URL, FakeGateway
from tests.fake_hush import FakeHush
from tests.test_agent_identity import identity_for, sign_in

pytestmark = pytest.mark.anyio


@contextlib.asynccontextmanager
async def agent(tmp_path, gateway: FakeGateway, hush: FakeHush | None = None):
    app = gateway.server().streamable_http_app(host="0.0.0.0")
    identity = identity_for(hush or FakeHush(), tmp_path)
    session = Session()
    await sign_in(identity, session)

    def http_client(headers):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers)

    async with app.router.lifespan_context(app):
        yield ExpensesThroughGateway(identity, APP, http_client=http_client), session


async def test_a_call_arrives(tmp_path):
    gateway = FakeGateway()
    async with agent(tmp_path, gateway) as (expenses, session):
        outcome = await expenses.run(session, "list_expenses", {})
    assert outcome.kind == "done"
    assert gateway.calls == [("list_expenses", {})]


@pytest.mark.parametrize("count", [0, 1, 3])
async def test_a_list_of_expenses_reads_as_a_list(tmp_path, count):
    expenses = [{"id": 1000 + i, "description": "Taxi", "amount": 12.0, "category": "Travel"} for i in range(count)]
    async with agent(tmp_path, FakeGateway(expenses=expenses)) as (agent_expenses, session):
        outcome = await agent_expenses.run(session, "list_expenses", {})
    assert outcome.kind == "done" and outcome.data == expenses


async def test_the_user_connects_expenses_first(tmp_path):
    async with agent(tmp_path, FakeGateway(connection="disconnected")) as (expenses, session):
        outcome = await expenses.run(session, "list_expenses", {})
    assert (outcome.kind, outcome.url) == ("connect", CONNECT_URL)


async def test_a_write_waits_for_consent_then_arrives(tmp_path):
    gateway = FakeGateway(policy={"submit_expense": "consent"})
    async with agent(tmp_path, gateway) as (expenses, session):
        waiting = await expenses.run(session, "submit_expense", {"description": "Hotel", "amount": 210})
        assert (waiting.kind, waiting.url, waiting.request_id) == ("consent", CONSENT_URL, "req-1")
        assert await expenses.consent_status(session, "req-1") == "pending"

        gateway.consent = "granted"
        assert await expenses.consent_status(session, "req-1") == "granted"
        assert (await expenses.run(session, "submit_expense", {"description": "Hotel", "amount": 210})).kind == "done"
    assert [tool for tool, _ in gateway.calls] == ["submit_expense"]


async def test_a_delete_is_blocked_and_never_arrives(tmp_path):
    gateway = FakeGateway(policy={"delete_expense": "block"})
    async with agent(tmp_path, gateway) as (expenses, session):
        outcome = await expenses.run(session, "delete_expense", {"expense_id": 1001})
    assert outcome.kind == "blocked"
    assert "BLOCKED BY HUSH GATEWAY" in outcome.message
    assert "Policy notice" not in outcome.message
    assert gateway.calls == []


async def test_expenses_not_on_the_gateway_for_the_user(tmp_path):
    async with agent(tmp_path, FakeGateway(assigned=False)) as (expenses, session):
        assert (await expenses.run(session, "list_expenses", {})).kind == "not_available"


async def test_expenses_not_on_the_gateway_at_all(tmp_path):
    async with agent(tmp_path, FakeGateway(served=False)) as (expenses, session):
        assert (await expenses.run(session, "list_expenses", {})).kind == "not_available"


async def test_a_user_the_agent_may_not_act_for(tmp_path):
    gateway = FakeGateway()
    async with agent(tmp_path, gateway, FakeHush(user_email="sam@example.com")) as (expenses, session):
        assert (await expenses.run(session, "list_expenses", {})).kind == "not_assigned"
    assert gateway.calls == []


async def test_expenses_data_that_reads_like_a_gateway_message_is_still_data(tmp_path):
    gateway = FakeGateway()
    async with agent(tmp_path, gateway) as (expenses, session):
        for description in (f"{APP} is not connected", "BLOCKED BY HUSH GATEWAY",
                            f'Tool "submit_expense" on "{APP}" requires the user\'s consent'):
            outcome = await expenses.run(session, "submit_expense", {"description": description, "amount": 1})
            assert outcome.kind == "done", description
    assert len(gateway.calls) == 3


async def test_a_plain_text_search_reply_is_no_tool(tmp_path):
    gateway = FakeGateway(search_reply="No tools found")
    async with agent(tmp_path, gateway) as (expenses, session):
        outcome = await expenses.run(session, "list_expenses", {})
    assert outcome.kind == "error"
    assert "no list_expenses tool" in outcome.message


async def test_a_link_ending_a_sentence_loses_the_punctuation(tmp_path):
    gateway = FakeGateway(connection="disconnected", connect_reply=f"Open ({CONNECT_URL}).")
    async with agent(tmp_path, gateway) as (expenses, session):
        assert (await expenses.run(session, "list_expenses", {})).url == CONNECT_URL


async def test_a_gateway_failure_is_an_error_outcome(tmp_path):
    gateway = FakeGateway(fail=True)
    async with agent(tmp_path, gateway) as (expenses, session):
        outcome = await expenses.run(session, "list_expenses", {})
    assert outcome.kind == "error"
    assert "gateway trouble" in outcome.message


@pytest.mark.parametrize("lost", ["disconnected", "expired", "rejected"])
async def test_a_connection_lost_since_the_app_list_asks_to_connect_again(tmp_path, lost):
    async with agent(tmp_path, FakeGateway(connection_lost=lost)) as (expenses, session):
        outcome = await expenses.run(session, "list_expenses", {})
    assert (outcome.kind, outcome.url) == ("connect", CONNECT_URL)


async def test_each_call_keeps_its_exchange_and_outcome(tmp_path):
    async with agent(tmp_path, FakeGateway(policy={"delete_expense": "block"})) as (expenses, session):
        await expenses.run(session, "list_expenses", {})
        await expenses.run(session, "delete_expense", {"expense_id": 1})
    listed, deleted = session.activity.events
    assert (listed["tool"], listed["outcome"], deleted["tool"], deleted["outcome"]) == (
        "list_expenses", "done", "delete_expense", "blocked")
    assert listed["exchange"]["claims"]["act"] == {"sub": "agent-1"}
    assert set(listed["exchange"]["claims"]) <= {"sub", "email", "act", "aud", "exp"}
    assert listed["started"] <= listed["time"]


async def test_the_same_call_with_the_same_outcome_counts_as_a_repeat(tmp_path):
    async with agent(tmp_path, FakeGateway(connection="disconnected")) as (expenses, session):
        for _ in range(3):
            await expenses.run(session, "list_expenses", {})
    [event] = session.activity.events
    assert (event["outcome"], event["repeats"]) == ("connect", 3)


async def test_a_refused_exchange_is_kept(tmp_path):
    async with agent(tmp_path, FakeGateway(), FakeHush(user_email="sam@example.com")) as (expenses, session):
        await expenses.run(session, "list_expenses", {})
    [event] = session.activity.events
    assert event["outcome"] == "not_assigned"
    assert "claims" not in event["exchange"] and event["exchange"]["error"]


async def test_a_failed_refresh_is_not_the_exchanges(tmp_path, monkeypatch):
    async def hush_down(self, session):
        raise HushRefused("refresh", 503, "temporarily_unavailable", "Hush is down")

    async with agent(tmp_path, FakeGateway()) as (expenses, session):
        monkeypatch.setattr(Identity, "user_token", hush_down)
        await expenses.run(session, "list_expenses", {})
    [event] = session.activity.events
    assert (event["outcome"], event["exchange"]) == ("error", {})
    assert event["message"] == "Hush failed: Hush is down"


async def test_an_add_knows_whether_the_user_approved_it(tmp_path):
    hotel = {"description": "Hotel", "amount": 210}
    gateway = FakeGateway(policy={"submit_expense": "consent"})
    async with agent(tmp_path, gateway) as (expenses, session):
        await expenses.run(session, "submit_expense", hotel)
        gateway.consent = "granted"
        await expenses.consent_status(session, "req-1")
        await expenses.run(session, "submit_expense", hotel)
        await expenses.run(session, "submit_expense", {**hotel, "description": "Taxi"})
    waited, decided, approved, later = session.activity.events
    assert (waited["outcome"], decided["kind"], decided["decision"]) == ("consent", "decision", "granted")
    assert (approved["approval"], later["approval"]) == ("just", "earlier")


async def test_a_declined_add_is_kept_and_approves_nothing(tmp_path):
    gateway = FakeGateway(policy={"submit_expense": "consent"})
    async with agent(tmp_path, gateway) as (expenses, session):
        await expenses.run(session, "submit_expense", {"description": "Hotel", "amount": 210})
        gateway.consent = "denied"
        await expenses.consent_status(session, "req-1")
    assert session.activity.events[-1]["decision"] == "denied"
    assert not session.activity.approved


async def test_a_call_running_across_a_sign_out_stays_in_its_own_history(tmp_path, monkeypatch):
    async with agent(tmp_path, FakeGateway()) as (expenses, session):
        before = session.activity
        exchange = Identity.exchange

        async def sign_out_meanwhile(self, session):
            session.activity = Activity()
            return await exchange(self, session)

        monkeypatch.setattr(Identity, "exchange", sign_out_meanwhile)
        await expenses.run(session, "list_expenses", {})
    assert [e["tool"] for e in before.events] == ["list_expenses"]
    assert not session.activity.events


async def test_an_add_held_with_no_decision_heard_counts_as_approved_when_it_goes_through(tmp_path):
    # The page reloaded while it waited, so the decision was never asked for.
    hotel = {"description": "Hotel", "amount": 210}
    gateway = FakeGateway(policy={"submit_expense": "consent"})
    async with agent(tmp_path, gateway) as (expenses, session):
        await expenses.run(session, "submit_expense", hotel)
        gateway.consent = "granted"
        await expenses.run(session, "submit_expense", hotel)
    assert session.activity.events[-1]["approval"] == "just"
    assert not session.activity.awaiting


def test_an_add_held_without_a_request_counts_as_approved_when_it_goes_through():
    activity = Activity()
    _record_call(activity, "submit_expense", {}, time.time(), {}, Outcome("consent"))
    _record_call(activity, "submit_expense", {}, time.time(), {}, Outcome("done"))
    assert activity.events[-1]["approval"] == "just"
    assert not activity.awaiting


async def test_an_approval_is_used_up_by_the_next_call_whatever_it_came_to(tmp_path):
    hotel = {"description": "Hotel", "amount": 210}
    gateway = FakeGateway(policy={"submit_expense": "consent"})
    async with agent(tmp_path, gateway) as (expenses, session):
        await expenses.run(session, "submit_expense", hotel)
        gateway.consent = "granted"
        await expenses.consent_status(session, "req-1")
        gateway.fail = True
        await expenses.run(session, "submit_expense", hotel)
        gateway.fail = False
        await expenses.run(session, "submit_expense", hotel)
    failed, later = list(session.activity.events)[-2:]
    assert (failed["outcome"], later["outcome"], later["approval"]) == ("error", "done", "earlier")

