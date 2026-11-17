"""What the tutorial page shows besides the agent's work: who the agent is to
Hush, and what Expenses saw."""

import datetime
import logging
import math
from urllib.parse import quote

import httpx2

from agent.identity import Identity, claims

log = logging.getLogger("agent")


def workload(identity: Identity) -> dict:
    """The agent's Kubernetes identity, as its token states it: what the agent
    is registered in Hush with."""
    try:
        token = claims(identity.service_account_token.read_text().strip())
    except (OSError, ValueError, IndexError):
        return {}
    audience = token.get("aud")
    return {
        "issuer": token.get("iss"),
        "subject": token.get("sub"),
        "audience": audience if isinstance(audience, list) else [audience] if audience else [],
    }


class ExpensesSide:
    """Expenses' address and access code, and the calls it got: read from its
    internal port, which only the agent reaches. And, from setup, whether Hush
    has it yet and whether it's assigned to the agent."""

    def __init__(self, http: httpx2.AsyncClient, internal_url: str | None, setup_url: str | None = None):
        self.http = http
        self.internal_url = internal_url.rstrip("/") if internal_url else None
        self.setup_url = setup_url.rstrip("/") if setup_url else None

    async def about(self) -> dict:
        return await self._get(self.internal_url, "/about", {})

    async def calls(self) -> list[dict]:
        """The Expenses service's log of the calls it got, on any account."""
        return [c for c in await self._get(self.internal_url, "/calls", []) if isinstance(c, dict)]

    async def in_hush(self) -> dict:
        return await self._get(self.setup_url, "/status", {"state": "unknown"})

    async def assigned_to(self, agent_id: str) -> dict:
        """Whether Expenses is assigned to the agent, as setup reads it."""
        return await self._get(self.setup_url, f"/assigned/{quote(agent_id, safe='')}",
                               {"assigned": None, "error": "the tutorial's setup is unreachable"})

    async def start_over(self, issuer: str | None) -> dict:
        """Has setup put Hush back as installed: how each step went. Setup
        finds the agent by its service account, from the agent's issuer."""
        unreachable = {"ok": False, "detail": "the tutorial's setup is unreachable"}
        # A dozen calls to Hush in a row, answered only once all are done.
        return await self._post(self.setup_url, "/start-over", {"issuer": issuer},
                                {"agent": unreachable, "app": unreachable}, timeout=120)

    async def reset(self) -> dict:
        """Has the Expenses service start as new."""
        done = await self._post(self.internal_url, "/reset", {}, {"ok": False})
        return ({"ok": True, "detail": "the sample expenses are back, and nobody is signed in to it"}
                if done.get("ok") else {"ok": False, "detail": "the Expenses service is unreachable"})

    async def _post(self, base: str | None, path: str, body: dict, default, timeout: float | None = None):
        if not base:
            return default
        try:
            extra = {"timeout": timeout} if timeout else {}
            return (await self.http.post(f"{base}{path}", json=body, **extra)).raise_for_status().json()
        except (httpx2.HTTPError, ValueError) as e:
            log.warning("posting to %s%s failed: %s", base, path, e)
            return default

    async def _get(self, base: str | None, path: str, default):
        if not base:
            return default
        try:
            return (await self.http.get(f"{base}{path}")).raise_for_status().json()
        except (httpx2.HTTPError, ValueError) as e:
            log.warning("reading %s%s failed: %s", base, path, e)
            return default


def pair_with_log(events: list[dict], calls: list[dict]) -> None:
    """Adds `seen` to each call that went through: the account the Expenses
    service acted on, which may not be the user's Hush email. The gateway
    passes nothing both sides could match on, so a call is paired by tool and
    time, with the service's log entries on any account. The call's own entry
    is always among them, so when they all name one account, it's this call's;
    when they don't, as when another user made the same call at the same time,
    the account stays unknown rather than maybe another user's."""
    def at(iso) -> float | None:
        try:
            return datetime.datetime.fromisoformat(iso).timestamp()
        except (TypeError, ValueError):
            return None

    logged = [(at(c.get("time")), c.get("tool"), c.get("account")) for c in calls]
    for event in events:
        # The log keeps whole seconds.
        earliest, latest = math.floor(at(event["started"])) - 1, at(event["time"]) + 2
        accounts = {account for when, tool, account in logged
                    if tool == event["tool"] and when is not None and earliest <= when <= latest}
        if len(accounts) == 1:
            event["seen"] = {"account": accounts.pop()}
