"""Puts Expenses in Hush for the tutorial, and takes it out again.

    python -m setup            create or update it, then report on SETUP_PORT
    python -m setup --remove   remove it (on uninstall)

HUSH_GATEWAY_ISSUER    The gateway's issuer: names Hush's API and the gateway
HUSH_API_KEY_ID        A Hush API key with applications:read, applications:write,
HUSH_API_KEY_SECRET    agents:read and agents:write
EXPENSES_NAME          Expenses' name in the Hush catalog (default expenses-tutorial)
EXPENSES_URL           Expenses' public address
EXPENSES_TUNNEL        Or: the quick tunnel's metrics address, to take it from
EXPENSES_SERVICE       Expenses' public port inside the cluster: Hush hears of
                       Expenses only once it answers there
AGENT_SUBJECT          The agent's service account, as its token names it:
                       starting over unregisters only agents signing in as it
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


async def answering(http: httpx2.AsyncClient, url: str, attempts: int = 60, interval: float = 2) -> None:
    """Returns once Expenses answers at url. Hush checks Expenses as soon as
    it learns its address, and the gateway keeps what that check found: a check
    made before Expenses is up leaves the gateway failing. Asked inside the
    cluster, so that its public address needn't be reachable from here."""
    for _ in range(attempts):
        try:
            if (await http.get(f"{url}/.well-known/oauth-protected-resource/mcp")).is_success:
                return
        except httpx2.HTTPError:
            pass
        log.info("waiting for Expenses at %s", url)
        await asyncio.sleep(interval)
    raise RuntimeError(f"Expenses doesn't answer at {url}")


class Setup:
    """Puts Expenses in Hush, then answers the agent's page about it."""

    def __init__(self, http: httpx2.AsyncClient, expenses_service: str | None = None,
                 agent_subject: str | None = None):
        self.http = http
        self.expenses_service = expenses_service
        self.agent_subject = agent_subject
        self.status = {"state": "working", "error": None}
        self.api: hush.HushAPI | None = None
        self.expenses: hush.Expenses | None = None
        self.app_id: str | None = None
        self.starting_over = asyncio.Lock()
        self.putting_back: asyncio.Task | None = None
        # The page asks every few seconds: a failure is logged when it changes.
        self.assigned_error: str | None = None

    async def put(self) -> None:
        """Until Hush has Expenses: a missing scope or a gateway still
        starting may clear up."""
        while True:
            try:
                if self.expenses is None:
                    self.api, self.expenses = await expenses_in_hush(self.http, with_url=True)
                expenses = self.expenses
                if self.expenses_service:
                    await answering(self.http, self.expenses_service)
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

    async def start_over(self, issuer: str | None) -> dict:
        """Puts Hush back as the tutorial installed it, for the reader to do
        the steps again: the agent unregistered, and Expenses' application on
        the gateway made anew, with no assignments and the org's default tool
        policies. Both steps run whatever the other came to; each reports
        how it went."""
        if self.status["state"] != "ready" or self.api is None or self.expenses is None:
            note = {"ok": False, "detail": "the tutorial is still putting the Expenses service in Hush"}
            return {"agent": note, "app": note}
        async with self.starting_over:
            return {"agent": await self.unregister(issuer), "app": await self.recreate()}

    async def unregister(self, issuer: str | None) -> dict:
        """Unregisters the agents signing in as the tutorial's agent, found by
        its service account rather than taken from the caller: the API key
        could unregister any agent. One that also signs in otherwise is
        someone's own, and left alone. So is one named as the page says but
        with no authentication yet: nothing tells it's the reader's, but it's
        likely, so the reader hears of it."""
        if not issuer or not self.agent_subject:
            return {"ok": False, "detail": "couldn't tell the agent's Kubernetes identity"}
        name_to_enter = self.agent_subject.rsplit(":", 1)[-1]
        gone, kept, unauthenticated = [], [], []
        try:
            for agent in await hush.service_agents(self.api):
                name, credentials = agent.get("name") or agent["id"], agent.get("credentials") or []
                if not credentials:
                    if agent.get("name") == name_to_enter:
                        unauthenticated.append(name)
                elif all(hush.signs_in_as(c, issuer, self.agent_subject) for c in credentials):
                    await hush.delete_agent(self.api, agent["id"])
                    gone.append(name)
                elif any(hush.signs_in_as(c, issuer, self.agent_subject) for c in credentials):
                    kept.append(name)
        except Exception as e:
            log.warning("unregistering the agent failed: %s", e)
            return {"ok": False, "detail": explain(e)}
        notes = [f"unregistered {', '.join(gone)}"] if gone else []
        if kept:
            notes.append(f"left {', '.join(kept)} alone: it signs in other ways too. Remove it in the Hush UI")
        if unauthenticated:
            notes.append(f"found {', '.join(unauthenticated)} with no authentication, so it was left in place. "
                         "Remove it in the Hush UI if you'd like to register it from scratch")
        if not notes:
            return {"ok": True, "detail": "it wasn't registered"}
        # Left in place isn't a failure: ok None, for the page to show neither ✓ nor ✕.
        return {"ok": False if kept else None if unauthenticated else True, "detail": "; ".join(notes)}

    async def recreate(self) -> dict:
        previous, self.app_id = self.app_id, None
        try:
            created = await hush.recreate(self.api, self.expenses)
        except hush.NotOurs as e:
            self.app_id = previous
            return {"ok": False, "detail": str(e)}
        except Exception as e:
            # It may be gone from the gateway: put it back, as when installing.
            log.warning("making Expenses' application anew failed, putting it back: %s", e)
            self.status.update(state="failed", error=explain(e))
            self.putting_back = asyncio.create_task(self.put())
            return {"ok": False, "detail": f"{explain(e)}. Start over again once Expenses is back in Hush"}
        self.app_id = created["id"]
        self.status.update(app=created.get("name"))
        return {"ok": True, "detail": "no assignments, and the default tool policies"}

    async def assigned_to(self, agent_id: str) -> dict:
        """Whether the reader assigned Expenses to the agent: None until Hush
        has Expenses, or when asking fails."""
        if self.app_id is None:
            return {"assigned": None, "error": None}
        try:
            assigned = await hush.assigned_to(self.api, self.app_id, agent_id)
        except Exception as e:
            error = explain(e)
            if error != self.assigned_error:
                log.warning("reading Expenses' assignments failed: %s", e)
            self.assigned_error = error
            return {"assigned": None, "error": error}
        self.assigned_error = None
        return {"assigned": assigned, "error": None}


def explain(e: Exception) -> str:
    if isinstance(e, hush.HushAPIError) and e.status == 403:
        return (f"{e}: does the API key have the applications:read, applications:write, agents:read and "
                "agents:write scopes?")
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

    async def start_over(request) -> JSONResponse:
        try:
            issuer = (await request.json()).get("issuer")
        except (ValueError, AttributeError):
            return JSONResponse({"error": "send a JSON object"}, status_code=400)
        return JSONResponse(await setup.start_over(issuer if isinstance(issuer, str) and issuer else None))

    return Starlette(routes=[Route("/status", show), Route("/ready", ready),
                             Route("/assigned/{agent_id}", assigned),
                             Route("/start-over", start_over, methods=["POST"])])


async def main() -> None:
    async with httpx2.AsyncClient(timeout=30) as http:
        setup = Setup(http, os.getenv("EXPENSES_SERVICE"), os.getenv("AGENT_SUBJECT"))
        server = uvicorn.Server(uvicorn.Config(status_app(setup), host="0.0.0.0", port=PORT))
        await asyncio.gather(server.serve(), setup.put())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")
    removing = sys.argv[1:] == ["--remove"]
    check_settings(with_url=not removing)
    asyncio.run(remove() if removing else main())
