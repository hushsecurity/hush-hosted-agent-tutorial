"""Expenses' own sign-in: a small OAuth authorization server.

Expenses doesn't know Hush or your company's sign-in. Like most SaaS apps,
it has its own accounts and issues its own tokens. The gateway connects to
it the standard MCP way (OAuth with PKCE, registering itself as a client),
and keeps your Expenses token for you.

The sign-in trusts the email you type, with no password: it's a demo. Type
your work email, so Expenses shows the same person as your company sign-in.

When Expenses is public, an access code keeps strangers out: the sign-in asks
for it as well, and only those who can see the tutorial page know it.
"""

import base64
import hashlib
import hmac
import html
import json
import re
import secrets
import time
from urllib.parse import parse_qs, unquote, urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

SIGN_IN_PATH = "/signin"
SIGN_IN_TTL = 10 * 60
CODE_TTL = 5 * 60
ACCESS_TTL = 60 * 60
REFRESH_TTL = 30 * 24 * 60 * 60
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Provider:
    """Sign-ins and tokens, in memory. Clients are remembered by no one: each
    client id carries the client's registration, signed with `client_key`, so
    with the same key a restarted Expenses still knows the gateway."""

    def __init__(self, base_url: str, client_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.client_key = hashlib.sha256(client_key.encode()).digest() if client_key else secrets.token_bytes(32)
        self.sign_ins: dict[str, tuple[str, AuthorizationParams, float]] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.access_tokens: dict[str, AccessToken] = {}
        self.refresh_tokens: dict[str, RefreshToken] = {}

    def forget(self) -> None:
        """Signs everyone out: every token it issued stops working. Registered
        clients stay, as their ids carry their own registration."""
        self.sign_ins.clear()
        self.codes.clear()
        self.access_tokens.clear()
        self.refresh_tokens.clear()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        registration, _, signature = client_id.partition(".")
        if not hmac.compare_digest(signature.encode(), self._sign("client", registration).encode()):
            return None
        try:
            fields = json.loads(_unb64(registration))
            client = OAuthClientInformationFull(**fields, client_id=client_id)
        except ValueError:
            return None
        if client.token_endpoint_auth_method != "none":
            client.client_secret = self._sign("secret", client_id)
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Gives the client its id and secret, both made from its registration."""
        fields = client_info.model_dump(mode="json", exclude_none=True, exclude={"client_id", "client_secret"})
        registration = _b64(json.dumps(fields, separators=(",", ":")).encode())
        client_info.client_id = f"{registration}.{self._sign('client', registration)}"
        if client_info.client_secret is not None:
            client_info.client_secret = self._sign("secret", client_info.client_id)

    def _sign(self, purpose: str, value: str) -> str:
        return _b64(hmac.new(self.client_key, f"{purpose}:{value}".encode(), hashlib.sha256).digest())

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Sends the browser to the sign-in page, which finishes the request."""
        request_id = secrets.token_urlsafe(24)
        self.sign_ins[request_id] = (client.client_id, params, time.time() + SIGN_IN_TTL)
        return f"{self.base_url}{SIGN_IN_PATH}?request={request_id}"

    def pending(self, request_id: str) -> bool:
        sign_in = self.sign_ins.get(request_id)
        return sign_in is not None and sign_in[2] >= time.time()

    def sign_in(self, request_id: str, email: str) -> str | None:
        """Signs `email` in for a pending request, and returns where to send the
        browser: back to the client, with a code."""
        pending = self.sign_ins.pop(request_id, None)
        if pending is None or pending[2] < time.time():
            return None
        client_id, params, _ = pending
        code = AuthorizationCode(
            code=secrets.token_urlsafe(32),
            scopes=params.scopes or [],
            expires_at=time.time() + CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=email,
        )
        self.codes[code.code] = code
        return construct_redirect_uri(str(params.redirect_uri), code=code.code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self.codes.get(authorization_code)
        if code is None or code.client_id != client.client_id or code.expires_at < time.time():
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self.codes.pop(authorization_code.code, None) is None:
            raise TokenError(error="invalid_grant", error_description="code already used")
        return self._issue(client.client_id, authorization_code.subject, authorization_code.scopes,
                           authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        token = self.refresh_tokens.get(refresh_token)
        if token is None or token.client_id != client.client_id or (token.expires_at or 0) < time.time():
            return None
        return token

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        self.refresh_tokens.pop(refresh_token.token, None)
        return self._issue(client.client_id, refresh_token.subject, scopes or refresh_token.scopes,
                           refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = self.access_tokens.get(token)
        if access is None or (access.expires_at or 0) < time.time():
            return None
        return access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.access_tokens.pop(token.token, None)
        self.refresh_tokens.pop(token.token, None)

    def _issue(self, client_id: str, email: str | None, scopes: list[str], resource: str | None) -> OAuthToken:
        now = int(time.time())
        access = AccessToken(token=secrets.token_urlsafe(32), client_id=client_id, scopes=scopes,
                             expires_at=now + ACCESS_TTL, resource=resource, subject=email)
        refresh = RefreshToken(token=secrets.token_urlsafe(32), client_id=client_id, scopes=scopes,
                               expires_at=now + REFRESH_TTL, resource=resource, subject=email)
        self.access_tokens[access.token] = access
        self.refresh_tokens[refresh.token] = refresh
        return OAuthToken(access_token=access.token, expires_in=ACCESS_TTL, refresh_token=refresh.token,
                          scope=" ".join(scopes) or None)


def sign_in_route(provider: Provider, access_code: str | None = None):
    """The sign-in page: asks for an email (and the access code, when there is
    one), then finishes the OAuth request."""
    access_code = (access_code or "").strip()
    ask_code = bool(access_code)

    async def handle(request: Request) -> Response:
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            if not provider.pending(request_id):
                return _page("This sign-in link expired. Start again from where you came.", status=400)
            return _page(form=request_id, ask_code=ask_code)
        form = await request.form()
        request_id, email = str(form.get("request", "")), str(form.get("email", "")).strip().lower()
        if not EMAIL.match(email):
            return _page("Enter an email address.", form=request_id, ask_code=ask_code, email=email, status=400)
        if ask_code and not secrets.compare_digest(str(form.get("code", "")).strip().encode(), access_code.encode()):
            return _page("That access code isn't right.", form=request_id, ask_code=ask_code, email=email, status=403)
        redirect = provider.sign_in(request_id, email)
        if redirect is None:
            return _page("This sign-in link expired. Start again from where you came.", status=400)
        return RedirectResponse(redirect, status_code=302)

    return handle


def _page(message: str = "", form: str | None = None, ask_code: bool = False, email: str = "",
          status: int = 200) -> HTMLResponse:
    body = f'<p class="error">{html.escape(message)}</p>' if message else ""
    if form is not None:
        body += f"""
<form method="post" action="{SIGN_IN_PATH}">
  <input type="hidden" name="request" value="{html.escape(form)}">
  <label for="email">Your work email</label>
  <input id="email" name="email" type="email" required autofocus placeholder="you@company.com"
         value="{html.escape(email)}">{_code_field() if ask_code else ""}
  <button type="submit">Sign in</button>
</form>
<p class="note">A demo app: no password. Use your company email.</p>"""
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in to Expenses</title>
<style>
  body {{ font: 16px/1.5 system-ui, sans-serif; background: #f5f6f8; color: #1d2330; margin: 0; }}
  main {{ max-width: 380px; margin: 12vh auto; padding: 32px; background: #fff; border-radius: 12px;
          box-shadow: 0 2px 12px rgba(0, 0, 0, .08); }}
  h1 {{ font-size: 22px; margin: 0 0 20px; }}
  label {{ display: block; font-weight: 600; margin-bottom: 6px; }}
  input[type=email], input[name=code] {{ width: 100%; box-sizing: border-box; padding: 10px; font: inherit;
                       border: 1px solid #c8ccd4; border-radius: 8px; }}
  button {{ margin-top: 16px; width: 100%; padding: 10px; font: inherit; font-weight: 600; color: #fff;
            background: #2f6fde; border: 0; border-radius: 8px; cursor: pointer; }}
  .second {{ margin-top: 16px; }}
  .note {{ color: #5b6475; font-size: 14px; margin-top: 20px; }}
  .error {{ color: #b42318; }}
</style></head>
<body><main><h1>Sign in to Expenses</h1>{body}</main></body></html>""", status_code=status)


def _code_field() -> str:
    return """
  <label for="code" class="second">Access code</label>
  <input id="code" name="code" required autocomplete="off" placeholder="Shown on the tutorial page">"""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class ClientIdFromBasicAuth:
    """Lets clients name themselves only in the token request's Basic header,
    as OAuth allows: the SDK also wants client_id in the form, so this copies
    it there."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers", [])) if scope["type"] == "http" else {}
        basic = headers.get(b"authorization", b"")
        if scope.get("path") != "/token" or not basic[:6].lower() == b"basic ":
            return await self.app(scope, receive, send)

        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        if "client_id" not in parse_qs(body.decode("latin-1")):
            try:
                client_id = base64.b64decode(basic[6:]).decode().split(":", 1)[0]
            except (ValueError, UnicodeDecodeError):
                client_id = ""
            if client_id:
                body += (b"&" if body else b"") + urlencode({"client_id": unquote(client_id)}).encode()
                scope = {**scope, "headers": [(k, v) for k, v in scope["headers"] if k != b"content-length"]
                         + [(b"content-length", str(len(body)).encode())]}

        sent = False

        async def replay():
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)
