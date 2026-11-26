"""The agent's front end: where you sign in for the agent and tell it what to do.

Each browser gets its own session (a cookie) once it starts signing in,
holding its own sign-in: the agent acts for whoever is signed in in the
browser that asks.

The tutorial page drives it all through the agent's API, from the browser:
the session cookie comes along by itself.
"""

import base64
import binascii
import dataclasses
import hmac
import logging
import math
import pathlib
import secrets
from urllib.parse import urlencode, urlsplit

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from agent.gateway import ExpensesThroughGateway, Outcome, record_sign_in
from agent.identity import Activity, HushRefused, Identity, Session
from agent.tutorial import ExpensesSide, pair_with_log, workload

log = logging.getLogger("agent")

SESSION_COOKIE = "agent_session"

PAGE = (pathlib.Path(__file__).parent / "page.html").read_text()


class BadInput(Exception):
    pass


class WithoutQueries(logging.Filter):
    """Drops the query from uvicorn's access log lines: /callback's carries
    the sign-in's authorization code."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, path, version, status = record.args
            record.args = (client, method, str(path).split("?", 1)[0], version, status)
        return True


class Sessions(BaseHTTPMiddleware):
    """Finds the browser's session by its cookie. A browser without one gets a
    new session, kept (and the cookie set) only once it starts signing in."""

    def __init__(self, app, secure: bool, sessions: dict[str, Session]):
        super().__init__(app)
        self.secure = secure
        self.sessions = sessions

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


class BasicAuth(BaseHTTPMiddleware):
    """Asks for a username and password, for a page others can reach. The load
    balancer's health check goes without."""

    def __init__(self, app, username: str, password: str):
        super().__init__(app)
        self.credentials = f"{username}:{password}".encode()

    def signed_in(self, request: Request) -> bool:
        scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            given = base64.b64decode(encoded.strip(), validate=True)
        except (binascii.Error, ValueError):
            return False
        return hmac.compare_digest(given, self.credentials)

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz" or self.signed_in(request):
            return await call_next(request)
        return PlainTextResponse("sign in needed\n", status_code=401,
                                 headers={"WWW-Authenticate": 'Basic realm="Hush tutorial", charset="UTF-8"'})


def refused(e: HushRefused) -> JSONResponse:
    return JSONResponse({
        "ok": False,
        "step": e.step,
        "not_assigned": e.not_assigned,
        "error": e.error,
        "message": e.description or e.error,
    }, status_code=403 if e.status in (400, 403) else e.status)


def back_to_page(e: HushRefused) -> RedirectResponse:
    """A refused sign-in, back on the page that started it: the browser came
    here by navigating, not from the page's script."""
    log.warning("sign-in refused: %s %s", e.error, e.description)
    return RedirectResponse(f"/?{urlencode({'signin_error': e.description or e.error})}")


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


def from_the_page(request: Request, agent_url: str) -> bool:
    """Whether a request came from the page itself, as served at agent_url:
    JSON, addressed to that host (not a name some other site points here), and
    sent from that origin. A page elsewhere can do none of these."""
    page = urlsplit(agent_url)
    hosts = {page.netloc.lower()}
    if page.hostname == "localhost":
        port = f":{page.port}" if page.port else ""
        hosts |= {f"127.0.0.1{port}", f"[::1]{port}"}
    host = request.headers.get("host", "").lower()
    origin = request.headers.get("origin")
    origin = origin.lower() if origin is not None else None
    json_sent = request.headers.get("content-type", "").split(";")[0].strip() == "application/json"
    return json_sent and host in hosts and (origin is None or origin == f"{page.scheme}://{host}")


def routes(identity: Identity, expenses: ExpensesThroughGateway, expenses_side: ExpensesSide,
           release: str | None, chart_version: str | None, agent_url: str,
           sessions: dict[str, Session]) -> list[Route]:
    async def index(request: Request) -> HTMLResponse:
        return HTMLResponse(PAGE, headers={"Cache-Control": "no-store"})

    async def setup(request: Request) -> JSONResponse:
        """What the page's setup steps show: the values to enter in Hush, and
        whether the reader has done them."""
        try:
            issuer, error = (await identity.gateway()).issuer, None
        except Exception as e:
            log.warning("reading the gateway's sign-in details failed: %s", e)
            issuer, error = None, str(e)
        agent_id = None
        try:
            registration = await identity.registration()
            agent_id = registration["id"]
            hush = {"id": agent_id, "name": registration["name"]}
        except HushRefused as e:
            # Most likely not registered yet: the reader's to do, not a failure.
            # `reason` is Hush's own words, under the agent's explanation.
            cause = e.__cause__ if isinstance(e.__cause__, HushRefused) else e
            hush = {"error": e.description or e.error, "reason": cause.description or cause.error,
                    "unknown": e.error == "invalid_client", "disabled": e.disabled}
        except Exception as e:
            log.warning("asking Hush about the agent failed: %s", e)
            hush = {"error": str(e), "reason": str(e), "unknown": False, "disabled": False}
        about = await expenses_side.about()
        assigned = await expenses_side.assigned_to(agent_id) if agent_id else {"assigned": None, "error": None}
        return JSONResponse({
            "release": release,
            "chart_version": chart_version,
            "agent": {**workload(identity), "hush": hush},
            "gateway": {"url": identity.gateway_url, "issuer": issuer, "error": error},
            "expenses": {"app": expenses.app, "url": about.get("url"), "access_code": about.get("access_code"),
                         "in_hush": await expenses_side.in_hush(), "assigned": assigned},
        })

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def sign_in(request: Request):
        try:
            return RedirectResponse(await identity.sign_in_url(request.state.session))
        except HushRefused as e:
            return back_to_page(e)

    async def callback(request: Request):
        params = request.query_params
        if "error" in params:
            return back_to_page(HushRefused("sign-in", 403, params["error"], params.get("error_description", "")))
        try:
            await identity.finish_sign_in(request.state.session, params.get("state", ""), params.get("code", ""))
        except HushRefused as e:
            return back_to_page(e)
        session = request.state.session
        session.activity = Activity()
        record_sign_in(session.activity, session.user.claims.get("email"))
        return RedirectResponse("/")

    async def me(request: Request) -> JSONResponse:
        user = request.state.session.user
        if user is None:
            return JSONResponse({"signed_in": False})
        return JSONResponse({"signed_in": True, "email": user.claims.get("email"), "name": user.claims.get("name")})

    async def sign_out(request: Request) -> JSONResponse:
        request.state.session.user = None
        request.state.session.activity = Activity()
        return JSONResponse({"ok": True})

    async def still_registered(step: dict | None) -> dict | None:
        """Setup's word that the agent is unregistered, checked with Hush: an
        agent registered in a way setup doesn't recognize still signs in."""
        if not step or step.get("ok") is False:
            return step
        try:
            registration = await identity.registration()
        except Exception:
            return step
        return {"ok": False, "detail": f"still registered as {registration['name'] or registration['id']}: "
                                       "remove it in the Hush UI"}

    async def start_over(request: Request) -> JSONResponse:
        """Puts the tutorial back to its start, for the reader to do it again:
        Hush as installed, the Expenses service as new, and everyone signed
        out of the assistant. Every step runs; each reports how it went."""
        if not from_the_page(request, agent_url):
            return JSONResponse({"ok": False, "message": "start over from the tutorial's page"}, status_code=403)
        hush = await expenses_side.start_over(workload(identity).get("issuer"))
        steps = {
            "agent": await still_registered(hush.get("agent")),
            "app": hush.get("app"),
            "expenses": await expenses_side.reset(),
            "assistant": {"ok": True, "detail": "everyone is signed out"},
        }
        sessions.clear()
        response = JSONResponse({"steps": steps})
        response.delete_cookie(SESSION_COOKIE)
        return response

    async def activity(request: Request) -> JSONResponse:
        """What the agent did for this browser's user, oldest first, each call
        that went through with what the Expenses service recorded of it."""
        events = [dict(e) for e in request.state.session.activity.events]
        went_through = [e for e in events if e.get("outcome") == "done"]
        if went_through:
            pair_with_log(went_through, await expenses_side.calls())
        return JSONResponse({"events": events})

    async def list_expenses(request: Request) -> JSONResponse:
        result = await expenses.run(request.state.session, "list_expenses", {})
        if result.kind == "done" and isinstance(result.data, dict):
            # One expense comes back as one text item: itself, not a list.
            result.data = [result.data]
        elif result.kind == "done" and not isinstance(result.data, list):
            result = Outcome("error", f"The Expenses service answered with something other than a list: {str(result.data)[:200]}")
        return outcome(result)

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
        Route("/api/setup", setup),
        Route("/api/me", me),
        Route("/api/signout", sign_out, methods=["POST"]),
        Route("/api/activity", activity),
        Route("/api/start-over", start_over, methods=["POST"]),
        Route("/api/expenses/list", list_expenses, methods=["POST"]),
        Route("/api/expenses/submit", submit_expense, methods=["POST"]),
        Route("/api/expenses/delete", delete_expense, methods=["POST"]),
        Route("/api/consent/{request_id}", consent),
    ]


async def bad_input(request: Request, e: BadInput) -> JSONResponse:
    return JSONResponse({"ok": False, "message": str(e)}, status_code=400)


def build(identity: Identity, expenses: ExpensesThroughGateway, agent_url: str,
          expenses_side: ExpensesSide | None = None, release: str | None = None,
          chart_version: str | None = None, basic_auth: tuple[str, str] | None = None) -> Starlette:
    """`agent_url` is where browsers reach the agent. `expenses_side` reads
    what Expenses saw, for the page. `release` and `chart_version` are the
    tutorial's Helm release and chart version, for the page's commands.
    `basic_auth`, a username and password, is asked for before anything else."""
    agent_url = agent_url.rstrip("/")
    expenses_side = expenses_side or ExpensesSide(identity.http, None)
    sessions: dict[str, Session] = {}
    middleware = [Middleware(BasicAuth, *basic_auth)] if basic_auth else []
    middleware.append(Middleware(Sessions, secure=agent_url.startswith("https://"), sessions=sessions))
    return Starlette(routes=routes(identity, expenses, expenses_side, release, chart_version, agent_url, sessions),
                     middleware=middleware, exception_handlers={BadInput: bad_input})
