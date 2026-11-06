import contextlib

import httpx2
import pytest

from agent.gateway import ExpensesThroughGateway
from agent.identity import Session
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


@pytest.mark.parametrize("lost", ["disconnected", "expired"])
async def test_a_connection_lost_since_the_app_list_asks_to_connect_again(tmp_path, lost):
    async with agent(tmp_path, FakeGateway(connection_lost=lost)) as (expenses, session):
        outcome = await expenses.run(session, "list_expenses", {})
    assert (outcome.kind, outcome.url) == ("connect", CONNECT_URL)
