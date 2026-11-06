"""A stand-in for the gateway's MCP tools, answering the way the gateway does,
enough to test how the agent reads its answers."""

import json

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

APP = "custom-expenses"
CONNECT_URL = "https://gateway.example.com/connect/abc"
CONSENT_URL = "https://hush.example.com/consent/xyz"
BLOCK_NOTICE = (
    "╔════════════════════════════════════════╗\n"
    "║        BLOCKED BY HUSH GATEWAY         ║\n"
    "╚════════════════════════════════════════╝\n\n"
    "delete_expense on custom-expenses is blocked by your organization's policy.\n\n"
)


class FakeGateway:
    def __init__(self, connection: str = "connected", assigned: bool = True, policy: dict | None = None,
                 search_reply: str | None = None, connect_reply: str = CONNECT_URL, fail: bool = False,
                 connection_lost: str | None = None, served: bool = True, expenses: list[dict] | None = None):
        self.connection = connection
        self.served = served
        self.assigned = assigned
        self.policy = policy or {}
        self.search_reply = search_reply
        self.connect_reply = connect_reply
        self.fail = fail
        self.connection_lost = connection_lost
        self.expenses = expenses
        self.consent = "pending"
        self.calls: list[tuple[str, dict]] = []

    def server(self) -> MCPServer:
        mcp = MCPServer("gateway")

        @mcp.tool(structured_output=False)
        def hush_list_apps(app: str = "") -> str:
            if self.fail:
                raise ToolError("gateway trouble")
            apps = [{"name": APP, "assigned": self.assigned, "connection": self.connection}] if self.served else []
            if app and app not in (a["name"] for a in apps):
                raise ToolError(f"unknown app: {app}")
            return json.dumps({"apps": apps})

        @mcp.tool(structured_output=False)
        def hush_search_tools(query: str, app: str = "") -> str:
            return self.search_reply or json.dumps([{"name": f"{APP}__{query}"}])

        @mcp.tool(structured_output=False)
        def hush_call_tool(name: str, arguments: dict) -> str | CallToolResult:
            tool = name.split("__")[-1]
            if self.connection_lost == "disconnected":
                return f'{APP} is not connected; call hush_connect with app="{APP}"'
            if self.connection_lost == "rejected":
                return (f'`{APP}` rejected the credentials it was sent. Call hush_connect with app="{APP}" '
                f"to reconnect, or verify them if `{APP}` uses static credentials.")
            if self.connection_lost == "expired":
                return f'{APP} connection expired or was revoked; call hush_connect with app="{APP}" to reconnect'
            rule = self.policy.get(tool)
            if rule == "block":
                return BLOCK_NOTICE + "Policy notice from the Hush security gateway ..."
            if rule == "consent" and self.consent != "granted":
                return (f'Tool "{tool}" on "{APP}" requires the user\'s consent before it can run.\n'
                        "1. Call the request_user_consent tool ...")
            self.calls.append((tool, arguments))
            if self.expenses is not None and tool == "list_expenses":
                # As Expenses answers a list: an item per expense, and the list wrapped.
                return CallToolResult(content=[TextContent(type="text", text=json.dumps(e)) for e in self.expenses],
                                      structured_content={"result": self.expenses})
            return json.dumps({"tool": tool, "arguments": arguments})

        @mcp.tool(structured_output=False)
        def hush_connect(app: str) -> str:
            return self.connect_reply

        @mcp.tool(structured_output=False)
        def hush_request_user_consent(app: str, tools: list[str]) -> str:
            return (f'Consent requested for {tools[0]} on "{app}". Ask the user to approve it by opening '
                    f"this URL in their browser:\n\n{CONSENT_URL}\n\n"
                    'Call the user_consent_status tool with request_id="req-1" and wait=true now, ...')

        @mcp.tool(structured_output=False)
        def hush_user_consent_status(request_id: str, wait: bool = False) -> str:
            return f'Consent request "{request_id}" for delete_expense on "{APP}": {self.consent}.'

        return mcp
