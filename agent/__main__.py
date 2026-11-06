"""Runs the agent and its front end.

    python -m agent

HUSH_GATEWAY_URL   The gateway's MCP URL: https://<gateway>/mcp
AGENT_URL          Where your browser reaches the agent (default http://localhost:8080)
AGENT_TOKEN_FILE   The pod's service account token, issued for the gateway
"""

import os
import pathlib

import httpx2
import uvicorn

from agent import web
from agent.identity import Identity

PORT = int(os.getenv("AGENT_PORT", "8080"))


def main() -> None:
    agent_url = os.getenv("AGENT_URL", f"http://localhost:{PORT}").rstrip("/")
    identity = Identity(
        http=httpx2.AsyncClient(timeout=30),
        gateway_url=os.environ["HUSH_GATEWAY_URL"],
        redirect_uri=f"{agent_url}/callback",
        service_account_token=pathlib.Path(os.getenv("AGENT_TOKEN_FILE", "/var/run/secrets/hush/token")),
    )
    uvicorn.run(web.build(identity, agent_url), host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
