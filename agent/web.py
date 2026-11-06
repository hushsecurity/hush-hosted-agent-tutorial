"""The agent's front end: where you sign in for the agent and tell it what to do.

Each browser gets its own session (a cookie) once it starts signing in,
holding its own sign-in: the agent acts for whoever is signed in in the
browser that asks.

For now, bare endpoints to open in a browser or with curl. The tutorial page
comes on top of them.
"""

import dataclasses
import html
import logging
import math
import secrets

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from agent.gateway import ExpensesThroughGateway, Outcome
from agent.identity import HushRefused, Identity, Session, claims

log = logging.getLogger("agent")

SESSION_COOKIE = "agent_session"
SHOWN_CLAIMS = ("sub", "email", "name", "act", "aud", "iss", "exp")

INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Hosted agent</title></head>
<body>
<h1>Hosted agent</h1>
<ul>
  <li><a href="/signin">Sign in for the agent</a></li>
  <li><a href="/api/me">Who is signed in</a></li>
  <li><a href="/api/exchange">Exchange tokens, and show what the agent's token says</a></li>
</ul>
{curl}
</body></html>"""


class BadInput(Exception):
    pass


class Sessions(BaseHTTPMiddleware):
    """Finds the browser's session by its cookie. A browser without one gets a
    new session, kept (and the cookie set) only once it starts signing in."""

    def __init__(self, app, secure: bool):
        super().__init__(app)
        self.secure = secure
        self.sessions: dict[str, Session] = {}

    async def dispatch(self, request: Request, call_next):
        session_id = request.cookies.get(SESSION_COOKIE)
        session = self.sessions.get(session_id) if session_id else None
        new = session is None
        if new:
            session_id, session = None, Session(forgotten=session_id is not None)
        request.state.session_id = session_id
        request.state.session = session
        response = await call_next(request)
        if new and (session.pending or session.user):
            session_id = secrets.token_urlsafe(32)
            session.forgotten = False
            self.sessions[session_id] = session
            # Lax: the cookie comes along when Hush sends the browser back to /callback.
            response.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="lax", secure=self.secure)
        return response


def refused(e: HushRefused) -> JSONResponse:
    return JSONResponse({
        "ok": False,
        "step": e.step,
        "not_assigned": e.not_assigned,
        "error": e.error,
        "message": e.description or e.error,
    }, status_code=403 if e.status in (400, 403) else e.status)


def failed(what: str, e: Exception) -> JSONResponse:
    log.exception("%s failed", what)
    return JSONResponse({"ok": False, "message": f"{what} failed: {e}"}, status_code=502)


def outcome(result: Outcome) -> JSONResponse:
    return JSONResponse(dataclasses.asdict(result))


async def body_of(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise BadInput("send a JSON object")
    if not isinstance(body, dict):
        raise BadInput("send a JSON object")
    return body


def amount_of(body: dict) -> float:
    try:
        amount = float(body["amount"])
    except (KeyError, TypeError, ValueError):
        raise BadInput("amount: a number is needed")
    if not math.isfinite(amount) or amount <= 0:
        raise BadInput("amount: must be more than zero")
    return amount


def expense_id_of(body: dict) -> int:
    value = body.get("expense_id")
    if isinstance(value, str) and value.strip().isdecimal():
        return int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise BadInput("expense_id: a whole number is needed")


def routes(identity: Identity, expenses: ExpensesThroughGateway, agent_url: str) -> list[Route]:
    async def index(request: Request) -> HTMLResponse:
        if request.state.session_id is None:
            return HTMLResponse(INDEX.format(curl="<p>Sign in, and this page shows curl commands that act as you.</p>"))
        session = f"-b {SESSION_COOKIE}={request.state.session_id}"
        curl = "\n".join([
            f"curl {session} {agent_url}/api/exchange",
            f"curl {session} -X POST {agent_url}/api/expenses/list",
            f"curl {session} -X POST {agent_url}/api/expenses/submit -d '{{\"description\": \"Hotel\", \"amount\": 210}}'",
            f"curl {session} -X POST {agent_url}/api/expenses/delete -d '{{\"expense_id\": 1001}}'",
            f"curl {session} {agent_url}/api/consent/<request id>",
        ])
        return HTMLResponse(INDEX.format(curl=f"<p>With curl, as this browser's session:</p><pre>{html.escape(curl)}</pre>"))

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def sign_in(request: Request):
        try:
            return RedirectResponse(await identity.sign_in_url(request.state.session))
        except HushRefused as e:
            return refused(e)

    async def callback(request: Request):
        params = request.query_params
        if "error" in params:
            return refused(HushRefused("sign-in", 403, params["error"], params.get("error_description", "")))
        try:
            await identity.finish_sign_in(request.state.session, params.get("state", ""), params.get("code", ""))
        except HushRefused as e:
            return refused(e)
        return RedirectResponse("/")

    async def me(request: Request) -> JSONResponse:
        user = request.state.session.user
        if user is None:
            return JSONResponse({"signed_in": False})
        return JSONResponse({"signed_in": True, "email": user.claims.get("email"), "name": user.claims.get("name")})

    async def sign_out(request: Request) -> JSONResponse:
        request.state.session.user = None
        return JSONResponse({"ok": True})

    async def exchange(request: Request) -> JSONResponse:
        try:
            token = await identity.exchange(request.state.session)
        except HushRefused as e:
            return refused(e)
        except Exception as e:
            return failed("the exchange", e)
        shown = {k: v for k, v in claims(token).items() if k in SHOWN_CLAIMS}
        return JSONResponse({"ok": True, "claims": shown})

    async def list_expenses(request: Request) -> JSONResponse:
        return outcome(await expenses.run(request.state.session, "list_expenses", {}))

    async def submit_expense(request: Request) -> JSONResponse:
        body = await body_of(request)
        return outcome(await expenses.run(request.state.session, "submit_expense", {
            "description": str(body.get("description") or "Expense"),
            "amount": amount_of(body),
            "category": str(body.get("category") or "Other"),
        }))

    async def delete_expense(request: Request) -> JSONResponse:
        body = await body_of(request)
        return outcome(await expenses.run(request.state.session, "delete_expense",
                                          {"expense_id": expense_id_of(body)}))

    async def consent(request: Request) -> JSONResponse:
        try:
            status = await expenses.consent_status(request.state.session, request.path_params["request_id"])
        except HushRefused as e:
            return refused(e)
        except Exception as e:
            return failed("the consent check", e)
        return JSONResponse({"status": status})

    return [
        Route("/", index),
        Route("/healthz", healthz),
        Route("/signin", sign_in),
        Route("/callback", callback),
        Route("/api/me", me),
        Route("/api/signout", sign_out, methods=["POST"]),
        Route("/api/exchange", exchange),
        Route("/api/expenses/list", list_expenses, methods=["POST"]),
        Route("/api/expenses/submit", submit_expense, methods=["POST"]),
        Route("/api/expenses/delete", delete_expense, methods=["POST"]),
        Route("/api/consent/{request_id}", consent),
    ]


async def bad_input(request: Request, e: BadInput) -> JSONResponse:
    return JSONResponse({"ok": False, "message": str(e)}, status_code=400)


def build(identity: Identity, expenses: ExpensesThroughGateway, agent_url: str) -> Starlette:
    """`agent_url` is where browsers reach the agent."""
    agent_url = agent_url.rstrip("/")
    return Starlette(routes=routes(identity, expenses, agent_url),
                     middleware=[Middleware(Sessions, secure=agent_url.startswith("https://"))],
                     exception_handlers={BadInput: bad_input})
