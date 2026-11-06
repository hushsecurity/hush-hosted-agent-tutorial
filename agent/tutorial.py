"""What the tutorial page shows besides the agent's work: who the agent is to
Hush, and what Expenses saw."""

import logging
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

    async def calls(self, account: str) -> list[dict]:
        """The calls made on `account` only: the page shows each user their own."""
        calls = await self._get(self.internal_url, "/calls", [])
        return [c for c in calls if isinstance(c, dict) and c.get("account") == account]

    async def in_hush(self) -> dict:
        return await self._get(self.setup_url, "/status", {"state": "unknown"})

    async def assigned_to(self, agent_id: str) -> dict:
        """Whether Expenses is assigned to the agent, as setup reads it."""
        return await self._get(self.setup_url, f"/assigned/{quote(agent_id, safe='')}",
                               {"assigned": None, "error": "the tutorial's setup is unreachable"})

    async def _get(self, base: str | None, path: str, default):
        if not base:
            return default
        try:
            return (await self.http.get(f"{base}{path}")).raise_for_status().json()
        except (httpx2.HTTPError, ValueError) as e:
            log.warning("reading %s%s failed: %s", base, path, e)
            return default
