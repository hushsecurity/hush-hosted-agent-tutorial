"""Runs the agent and its front end.

    python -m agent

HUSH_GATEWAY_URL   The gateway's MCP URL: https://<gateway>/mcp
AGENT_URL          Where your browser reaches the agent (default http://localhost:8080)
AGENT_TOKEN_FILE   The pod's service account token, issued for the gateway
EXPENSES_APP       Expenses' name on the gateway (default custom-expenses)
EXPENSES_INTERNAL  Expenses' internal address, for the page to show what it saw
SETUP_INTERNAL     Setup's address, for the page to show whether Hush has Expenses
HELM_RELEASE       The tutorial's Helm release, for the page's clean-up command
"""

import logging
import os
import pathlib
import sys

import httpx2
import uvicorn

from agent import web
from agent.gateway import ExpensesThroughGateway
from agent.identity import Identity
from agent.tutorial import ExpensesSide

PORT = int(os.getenv("AGENT_PORT", "8080"))


def main() -> None:
    if not os.getenv("HUSH_GATEWAY_URL"):
        sys.exit(f"Set HUSH_GATEWAY_URL.\n{__doc__}")
    agent_url = os.getenv("AGENT_URL", f"http://localhost:{PORT}").rstrip("/")
    http = httpx2.AsyncClient(timeout=30)
    identity = Identity(
        http=http,
        gateway_url=os.environ["HUSH_GATEWAY_URL"],
        redirect_uri=f"{agent_url}/callback",
        service_account_token=pathlib.Path(os.getenv("AGENT_TOKEN_FILE", "/var/run/secrets/hush/token")),
    )
    expenses = ExpensesThroughGateway(identity, os.getenv("EXPENSES_APP", "custom-expenses"))
    expenses_side = ExpensesSide(http, os.getenv("EXPENSES_INTERNAL"), os.getenv("SETUP_INTERNAL"))
    config = uvicorn.Config(web.build(identity, expenses, agent_url, expenses_side, os.getenv("HELM_RELEASE")),
                            host="0.0.0.0", port=PORT)
    # After the Config: it sets up uvicorn's loggers.
    logging.getLogger("uvicorn.access").addFilter(web.WithoutQueries())
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
