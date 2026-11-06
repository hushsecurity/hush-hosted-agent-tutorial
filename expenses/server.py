"""The Expenses MCP server: the app the agent works on.

Three tools, one of each kind a gateway policy tells apart: list (read),
submit (write) and delete (destructive).
"""

import dataclasses
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from expenses.store import Store

GUEST = "guest"

Amount = Annotated[float, Field(gt=0, allow_inf_nan=False)]


def current_account() -> str:
    return GUEST


def build(store: Store) -> MCPServer:
    mcp = MCPServer("Expenses", instructions="Your expense reports: list, submit and delete expenses.")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_expenses() -> list[dict]:
        """List your expenses."""
        account = current_account()
        expenses = [dataclasses.asdict(e) for e in store.expenses(account)]
        store.record(account, "list_expenses", {}, f"{len(expenses)} expenses")
        return expenses

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def submit_expense(description: str, amount: Amount, category: str = "Other") -> dict:
        """Submit a new expense."""
        account = current_account()
        expense = store.submit(account, description, amount, category)
        store.record(
            account,
            "submit_expense",
            {"description": description, "amount": amount, "category": category},
            f"submitted #{expense.id}",
        )
        return dataclasses.asdict(expense)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def delete_expense(expense_id: int) -> str:
        """Delete one of your expenses."""
        account = current_account()
        deleted = store.delete(account, expense_id)
        result = f"deleted #{expense_id}" if deleted else f"no expense #{expense_id}"
        store.record(account, "delete_expense", {"expense_id": expense_id}, result)
        return result

    return mcp
