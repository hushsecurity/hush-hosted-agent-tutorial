"""The agent's front end: where you sign in for the agent and tell it what to do.

Each browser gets its own session (a cookie) once it starts signing in,
holding its own sign-in: the agent acts for whoever is signed in in the
browser that asks.

For now, bare endpoints to open in a browser or with curl. The tutorial page
comes on top of them.
"""

import html
import logging
import secrets

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

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


def routes(identity: Identity, agent_url: str) -> list[Route]:
    async def index(request: Request) -> HTMLResponse:
        if request.state.session_id is None:
            return HTMLResponse(INDEX.format(curl="<p>Sign in, and this page shows curl commands that act as you.</p>"))
        curl = f"curl -b {SESSION_COOKIE}={request.state.session_id} {agent_url}/api/exchange"
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

    return [
        Route("/", index),
        Route("/healthz", healthz),
        Route("/signin", sign_in),
        Route("/callback", callback),
        Route("/api/me", me),
        Route("/api/signout", sign_out, methods=["POST"]),
        Route("/api/exchange", exchange),
    ]


def build(identity: Identity, agent_url: str) -> Starlette:
    """`agent_url` is where browsers reach the agent."""
    agent_url = agent_url.rstrip("/")
    return Starlette(routes=routes(identity, agent_url),
                     middleware=[Middleware(Sessions, secure=agent_url.startswith("https://"))])
