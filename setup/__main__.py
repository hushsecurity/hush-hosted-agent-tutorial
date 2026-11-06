"""Puts Expenses in Hush for the tutorial, and takes it out again.

    python -m setup            create or update it, then report on SETUP_PORT
    python -m setup --remove   remove it (on uninstall)

HUSH_GATEWAY_ISSUER    The gateway's issuer: names Hush's API and the gateway
HUSH_API_KEY_ID        A Hush API key with applications:read and
HUSH_API_KEY_SECRET    applications:write
EXPENSES_NAME          Expenses' name in the Hush catalog (default expenses-tutorial)
EXPENSES_URL           Expenses' public address
EXPENSES_TUNNEL        Or: the quick tunnel's metrics address, to take it from
"""

import asyncio
import logging
import os
import sys

import httpx2
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from expenses import tunnel
from setup import hush

log = logging.getLogger("setup")
PORT = int(os.getenv("SETUP_PORT", "8002"))
RETRY_SECONDS = 15


def check_settings(with_url: bool) -> None:
    missing = [n for n in ("HUSH_GATEWAY_ISSUER", "HUSH_API_KEY_ID", "HUSH_API_KEY_SECRET") if not os.getenv(n)]
    if with_url and not (os.getenv("EXPENSES_URL") or os.getenv("EXPENSES_TUNNEL")):
        missing.append("EXPENSES_URL or EXPENSES_TUNNEL")
    if missing:
        sys.exit(f"Set {', '.join(missing)}.\n{__doc__}")
    try:
        hush.api_and_deployment(os.environ["HUSH_GATEWAY_ISSUER"])
    except ValueError as e:
        sys.exit(f"HUSH_GATEWAY_ISSUER: {e}")


async def expenses_in_hush(http: httpx2.AsyncClient, with_url: bool) -> tuple[hush.HushAPI, hush.Expenses]:
    api_url, deployment = hush.api_and_deployment(os.environ["HUSH_GATEWAY_ISSUER"])
    api = hush.HushAPI(http, api_url, os.environ["HUSH_API_KEY_ID"], os.environ["HUSH_API_KEY_SECRET"])
    expenses = hush.Expenses(os.getenv("EXPENSES_NAME", "expenses-tutorial"), deployment)
    if with_url:
        url = os.getenv("EXPENSES_URL") or await tunnel.public_address(http, os.environ["EXPENSES_TUNNEL"])
        expenses.url = url.rstrip("/")
    return api, expenses


class Setup:
    """Puts Expenses in Hush, then answers the agent's page about it."""

    def __init__(self, http: httpx2.AsyncClient):
        self.http = http
        self.status = {"state": "working", "error": None}
        self.api: hush.HushAPI | None = None
        self.app_id: str | None = None

    async def put(self) -> None:
        """Until Hush has Expenses: a missing scope or a gateway still
        starting may clear up."""
        expenses = None
        while True:
            try:
                if expenses is None:
                    self.api, expenses = await expenses_in_hush(self.http, with_url=True)
                app = await hush.put(self.api, expenses)
            except Exception as e:
                log.warning("putting Expenses in Hush failed, retrying: %s", e)
                self.status.update(state="failed", error=explain(e))
                await asyncio.sleep(RETRY_SECONDS)
                continue
            log.info("Expenses is in Hush as %s, on the gateway as %s", expenses.name, app.get("name"))
            if app.get("name") != expenses.catalog_id:
                log.warning("the agent calls Expenses as %s, but the gateway has it as %s",
                            expenses.catalog_id, app.get("name"))
            self.app_id = app["id"]
            self.status.update(state="ready", error=None, catalog=expenses.name, app=app.get("name"),
                               display_name=hush.DISPLAY_NAME, url=expenses.url)
            return

    async def assigned_to(self, agent_id: str) -> dict:
        """Whether the reader assigned Expenses to the agent: None until Hush
        has Expenses, or when asking fails."""
        if self.app_id is None:
            return {"assigned": None, "error": None}
        try:
            return {"assigned": await hush.assigned_to(self.api, self.app_id, agent_id), "error": None}
        except Exception as e:
            log.warning("reading Expenses' assignments failed: %s", e)
            return {"assigned": None, "error": explain(e)}


def explain(e: Exception) -> str:
    if isinstance(e, hush.HushAPIError) and e.status == 403:
        return f"{e}: does the API key have the applications:read and applications:write scopes?"
    return str(e)


async def remove() -> None:
    """Fails loudly, so the uninstall says so: an expired key leaves Expenses
    in Hush."""
    try:
        async with httpx2.AsyncClient(timeout=30) as http:
            api, expenses = await expenses_in_hush(http, with_url=False)
            await hush.remove(api, expenses)
    except hush.NotOurs as e:
        log.warning("left it alone: %s", e)
        return
    except Exception as e:
        release, namespace = os.getenv("HELM_RELEASE", "<release>"), os.getenv("HELM_NAMESPACE", "<namespace>")
        log.error("removing Expenses from Hush failed: %s. Remove it in the Hush UI, then: "
                  "helm uninstall %s -n %s --no-hooks", explain(e), release, namespace)
        sys.exit(1)
    log.info("removed Expenses from Hush")


def status_app(setup: Setup) -> Starlette:
    async def show(request) -> JSONResponse:
        return JSONResponse(setup.status)

    async def ready(request) -> JSONResponse:
        return JSONResponse(setup.status, status_code=200 if setup.status["state"] == "ready" else 503)

    async def assigned(request) -> JSONResponse:
        return JSONResponse(await setup.assigned_to(request.path_params["agent_id"]))

    return Starlette(routes=[Route("/status", show), Route("/ready", ready),
                             Route("/assigned/{agent_id}", assigned)])


async def main() -> None:
    async with httpx2.AsyncClient(timeout=30) as http:
        setup = Setup(http)
        server = uvicorn.Server(uvicorn.Config(status_app(setup), host="0.0.0.0", port=PORT))
        await asyncio.gather(server.serve(), setup.put())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")
    removing = sys.argv[1:] == ["--remove"]
    check_settings(with_url=not removing)
    asyncio.run(remove() if removing else main())
