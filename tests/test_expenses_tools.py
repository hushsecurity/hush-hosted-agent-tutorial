import collections
import json

import pytest
from mcp import Client

from expenses import server
from expenses.store import SAMPLE_EXPENSES, Store

pytestmark = pytest.mark.anyio


async def call(client: Client, tool: str, **arguments):
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result
    structured = result.structured_content
    if structured is None:
        text = result.content[0].text
        return json.loads(text) if text.startswith("{") else text
    return structured.get("result", structured)


async def test_tools_list_submit_and_delete():
    store = Store()
    async with Client(server.build(store)) as client:
        listed = await call(client, "list_expenses")
        assert len(listed) == len(SAMPLE_EXPENSES)

        submitted = await call(client, "submit_expense", description="Hotel", amount=210.0, category="Travel")
        assert submitted["description"] == "Hotel"

        assert await call(client, "delete_expense", expense_id=submitted["id"]) == f"deleted #{submitted['id']}"
        assert len(await call(client, "list_expenses")) == len(SAMPLE_EXPENSES)

    assert [c.tool for c in store.calls] == ["list_expenses", "submit_expense", "delete_expense", "list_expenses"]


async def test_tools_are_annotated_by_kind():
    async with Client(server.build(Store())) as client:
        tools = {t.name: t.annotations for t in (await client.list_tools()).tools}
    assert tools["list_expenses"].read_only_hint
    assert not tools["submit_expense"].read_only_hint and not tools["submit_expense"].destructive_hint
    assert tools["delete_expense"].destructive_hint


@pytest.mark.parametrize("amount", [-5, 0, float("inf"), float("nan")])
async def test_submit_refuses_amounts_that_arent_positive(amount):
    store = Store()
    async with Client(server.build(store)) as client:
        result = await client.call_tool("submit_expense", {"description": "Odd", "amount": amount})
    assert result.is_error
    assert store.calls == collections.deque()
