"""The agent's work: calling Expenses through the gateway, for the user.

The agent never talks to Expenses directly, and never sees the user's
Expenses token. It calls the gateway with the token naming both the user and
the agent, and the gateway checks each call, then calls Expenses with the
user's own Expenses connection.

The gateway serves any MCP client. Its tools (hush_list_apps,
hush_search_tools, hush_call_tool, ...) answer in plain words meant for an
LLM; this scripted agent reads the few answers it needs to tell apart.
"""

import contextlib
import dataclasses
import datetime
import itertools
import json
import logging
import re
import time
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from agent.identity import Activity, HushRefused, Identity, Session, claims

log = logging.getLogger("agent")


class _SessionEndAccepted(logging.Filter):
    """The gateway accepts the end of an MCP session with 202, which the MCP
    client reports as a failure: it isn't one."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage() != "Session termination failed: 202"


logging.getLogger("mcp.client.streamable_http").addFilter(_SessionEndAccepted())

URL = re.compile(r"https?://\S+")
# What the page shows of the token each exchange gave: never the token itself.
EXCHANGE_CLAIMS = ("sub", "email", "act", "aud", "exp")
_event_ids = itertools.count(1)
URL_TRAILER = ".,;:!?)]}>'\""
CONSENT_NEEDED = re.compile(r'\s*Tool "([^"]+)" on "([^"]+)" requires the user\'s consent')
CONSENT_STATUS = re.compile(r": (granted|denied|pending)\.\s*$")
BLOCKED_BANNER = "╔"
BLOCKED = "BLOCKED BY HUSH GATEWAY"
POLICY_NOTICE = "Policy notice from"


@dataclasses.dataclass
class Outcome:
    """What happened to one call.

    kind is one of:
      done           Expenses answered: `data`
      connect        the user connects Expenses once, at `url`
      consent        the call waits for the user's approval of `request_id`,
                     at `url` when there is one
      blocked        the gateway's policy blocks the call
      not_assigned   Hush refused the exchange: the agent may not act for the user
      signed_out     the user's sign-in is gone (the agent restarted, the tutorial started over, or it expired)
      not_available  Expenses isn't on the gateway for this user and agent
      refused        Hush refused something else
      error          anything else
    """

    kind: str
    message: str = ""
    data: Any = None
    url: str | None = None
    request_id: str | None = None


class ExpensesThroughGateway:
    def __init__(self, identity: Identity, app: str, http_client=create_mcp_http_client):
        self.identity = identity
        self.app = app
        self.http_client = http_client

    async def run(self, session: Session, tool: str, arguments: dict) -> Outcome:
        activity = session.activity
        started = time.time()
        exchanged: dict = {}
        outcome = await self._attempt(session, tool, arguments, exchanged)
        exchanged.pop("asked", None)
        _record_call(activity, tool, arguments, started, exchanged, outcome)
        return outcome

    async def _attempt(self, session: Session, tool: str, arguments: dict, exchanged: dict) -> Outcome:
        try:
            return await self._run(session, tool, arguments, exchanged)
        except HushRefused as e:
            if e.step == "exchange":
                exchanged["error"] = e.description or e.error
            if e.error == "login_required":
                return Outcome("signed_out", e.description or e.error)
            if e.status >= 500:
                return Outcome("error", f"Hush failed: {e.description or e.error}")
            return Outcome("not_assigned" if e.not_assigned else "refused", e.description or e.error)
        except Exception as e:
            log.exception("%s through the gateway failed", tool)
            if exchanged.get("asked") and "claims" not in exchanged:
                exchanged["error"] = str(_cause(e))
            return Outcome("error", f"{tool} through the gateway failed: {_cause(e)}")

    async def _run(self, session: Session, tool: str, arguments: dict, exchanged: dict) -> Outcome:
        # The user's sign-in first, refreshed if need be: its failures aren't the exchange's.
        await self.identity.user_token(session)
        exchanged["asked"] = True
        token = await self.identity.exchange(session)
        exchanged["claims"] = {k: v for k, v in claims(token).items() if k in EXCHANGE_CLAIMS}
        async with self._gateway(token) as gateway:
            app = await self._app(gateway)
            if app is None or not app.get("assigned"):
                return Outcome("not_available", f"{self.app} isn't available on the gateway for you and this agent")
            if app.get("connection") in ("disconnected", "invalid"):
                return await self._connect(gateway)
            if app.get("connection") == "unavailable":
                return Outcome("error", app.get("reason") or f"the gateway can't reach {self.app}")
            name = await self._tool_name(gateway, tool)
            if name is None:
                return Outcome("error", f"the gateway lists no {tool} tool on {self.app}")
            result = await gateway.call_tool("hush_call_tool", {"name": name, "arguments": arguments})
            return await self._outcome(gateway, result)

    async def consent_status(self, session: Session, request_id: str) -> str:
        """granted, denied, pending, or expired. After granted, run the call
        again: the next exchange carries the approval."""
        activity = session.activity
        async with self._gateway(await self.identity.exchange(session)) as gateway:
            text = _text(await gateway.call_tool(
                "hush_user_consent_status", {"request_id": request_id, "wait": False}))
        status = match.group(1) if (match := CONSENT_STATUS.search(text)) else "expired"
        if status != "pending" and request_id in activity.awaiting:
            _record_decision(activity, request_id, status)
        return status

    @contextlib.asynccontextmanager
    async def _gateway(self, token: str):
        http = self.http_client(headers={"Authorization": f"Bearer {token}"})
        async with http, streamable_http_client(self.identity.gateway_url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def _app(self, gateway: ClientSession) -> dict | None:
        # All of them: the gateway refuses to look up an app it doesn't serve.
        result = await gateway.call_tool("hush_list_apps", {})
        if result.is_error:
            raise RuntimeError(_text(result))
        listed = _json(result)
        apps = listed.get("apps", []) if isinstance(listed, dict) else []
        return next((a for a in apps if a.get("name") == self.app), None)

    async def _tool_name(self, gateway: ClientSession, tool: str) -> str | None:
        """The gateway's name for the tool, found the way any agent finds it."""
        found = _json(await gateway.call_tool("hush_search_tools", {"app": self.app, "query": tool}))
        if isinstance(found, dict):
            found = found.get("tools")
        if not isinstance(found, list):
            return None
        names = (t.get("name") for t in found if isinstance(t, dict))
        return next((n for n in names if isinstance(n, str) and n.endswith(tool)), None)

    async def _connect(self, gateway: ClientSession) -> Outcome:
        text = _text(await gateway.call_tool("hush_connect", {"app": self.app}))
        return Outcome("connect", f"Connect your {self.app} account once", url=_url(text))

    async def _outcome(self, gateway: ClientSession, result) -> Outcome:
        """The gateway's own replies open with what happened; anything else is
        Expenses' answer, whatever its words."""
        text = _text(result)
        head = text.lstrip()
        if head.startswith(BLOCKED_BANNER) and BLOCKED in "\n".join(head.splitlines()[:3]):
            return Outcome("blocked", text.split(POLICY_NOTICE)[0].strip())
        if consent := CONSENT_NEEDED.match(text):
            return await self._request_consent(gateway, tool=consent.group(1), app=consent.group(2))
        if text.startswith((f"{self.app} is not connected", f"{self.app} connection expired",
                            f"`{self.app}` rejected the credentials")):
            return await self._connect(gateway)
        if result.is_error:
            return Outcome("error", text)
        return Outcome("done", data=_json(result))

    async def _request_consent(self, gateway: ClientSession, tool: str, app: str) -> Outcome:
        text = _text(await gateway.call_tool("hush_request_user_consent", {"app": app, "tools": [tool]}))
        request_id = re.search(r'request_id="([^"]+)"', text)
        return Outcome("consent", f"{tool} waits for your approval", url=_url(text.split("Call the")[0]),
                       request_id=request_id.group(1) if request_id else None)


def _cause(error: BaseException) -> BaseException:
    """The error itself, out of the groups the MCP client's tasks wrap it in."""
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


def _url(text: str) -> str | None:
    """The first link in a reply, without punctuation the sentence put after it."""
    match = URL.search(text)
    return match.group(0).rstrip(URL_TRAILER) if match else None


def _text(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def _json(result) -> Any:
    """A tool's answer as data. A tool returning a list may answer with one
    text item per element, or with the list wrapped as {"result": [...]}."""
    structured = result.structured_content
    if isinstance(structured, dict) and list(structured) == ["result"]:
        return structured["result"]
    if structured is not None:
        return structured
    items = [_parse(c.text) for c in result.content if getattr(c, "type", "") == "text"]
    return items[0] if len(items) == 1 else items


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _record_call(activity: Activity, tool: str, arguments: dict, started: float, exchanged: dict,
                 outcome: Outcome) -> None:
    """Keeps what one call came to, for the page: the exchange it began with,
    what the gateway made of it, and for a call that went through, whether the
    user approved it just before, or earlier. The same call with the same
    outcome again, as when the page checks whether the user connected yet,
    counts as a repeat."""
    event_id = next(_event_ids)
    approval = None
    if outcome.kind == "done":
        # Held for approval with no decision heard, as when the page reloaded
        # while it waited: going through now means the user approved it.
        held = [request for request, (held_tool, _) in activity.awaiting.items() if held_tool == tool]
        for request in held:
            del activity.awaiting[request]
        if held:
            activity.approved.add(tool)
        if held or tool in activity.just_approved:
            approval = "just"
        elif tool in activity.approved:
            approval = "earlier"
    # The call after an approval uses it up, whatever it came to.
    activity.just_approved.discard(tool)
    if outcome.kind == "consent":
        activity.awaiting[outcome.request_id or f"unnamed-{event_id}"] = (tool, arguments)
    event = {
        "id": event_id, "kind": "call", "tool": tool, "arguments": arguments, "started": _iso(started),
        "time": _iso(time.time()), "exchange": exchanged, "outcome": outcome.kind, "message": outcome.message,
        "approval": approval, "repeats": 1,
    }
    same = ("kind", "tool", "arguments", "outcome", "message")
    last = activity.events[-1] if activity.events else None
    if last and all(last.get(k) == event[k] for k in same):
        activity.events[-1] = {**event, "id": last["id"], "repeats": last["repeats"] + 1}
    else:
        activity.events.append(event)


def _record_decision(activity: Activity, request_id: str, status: str) -> None:
    """The user's answer to a call waiting for approval: granted, denied, or expired."""
    tool, arguments = activity.awaiting.pop(request_id)
    if status == "granted":
        activity.approved.add(tool)
        activity.just_approved.add(tool)
    activity.events.append({"id": next(_event_ids), "kind": "decision", "tool": tool, "arguments": arguments,
                            "decision": status, "time": _iso(time.time())})


def _iso(seconds: float) -> str:
    return datetime.datetime.fromtimestamp(seconds, datetime.UTC).isoformat(timespec="milliseconds")
