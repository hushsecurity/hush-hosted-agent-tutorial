"""The Expenses MCP server: the app the agent works on.

Three tools, one of each kind a gateway policy tells apart: list (read),
submit (write) and delete (destructive).
"""

import dataclasses
from typing import Annotated

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette

from expenses import auth
from expenses.store import Store

Amount = Annotated[float, Field(gt=0, allow_inf_nan=False)]


def current_account() -> str:
    """The email of the account the caller's token was issued to."""
    return get_access_token().subject


def build(store: Store, base_url: str, access_code: str | None = None, client_key: str | None = None) -> MCPServer:
    """`base_url` is Expenses' public address: what the gateway, and your
    browser to sign in, reach it at. With `access_code`, signing in asks for
    it too. With `client_key`, clients stay registered across restarts."""
    base_url = base_url.rstrip("/")
    provider = auth.Provider(base_url, client_key)
    mcp = MCPServer(
        "Expenses",
        instructions="Your expense reports: list, submit and delete expenses.",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=base_url,
            resource_server_url=f"{base_url}/mcp",
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
            # Expenses' tokens are opaque and live only here, so a token it
            # knows was issued for it.
            validate_token_resource=False,
        ),
    )
    mcp.custom_route(auth.SIGN_IN_PATH, methods=["GET", "POST"])(auth.sign_in_route(provider, access_code))

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


def app(store: Store, base_url: str, access_code: str | None = None, client_key: str | None = None) -> Starlette:
    """Expenses as served: the MCP server and its sign-in."""
    # host="0.0.0.0" also turns off the SDK's localhost-only Host check: in
    # the cluster, the gateway reaches Expenses by its public name. Plain JSON
    # answers rather than event streams, which some tunnels hold back.
    served = build(store, base_url, access_code, client_key).streamable_http_app(host="0.0.0.0", json_response=True)
    served.add_middleware(auth.ClientIdFromBasicAuth)
    return served
