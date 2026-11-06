"""The agent's identity: signing a user in, and the token exchange.

The agent has no secret of its own. Kubernetes gives its pod a service
account token, issued for the gateway, and Hush trusts the cluster's issuer.

To act for a user, the agent needs to know who the user is: the user signs in
to Hush for this agent (OAuth with PKCE, `resource` naming the agent). The
agent learns its own name from Hush, with its service account token. It then
exchanges the user's token together with its own (RFC 8693 token exchange)
for one token naming both. Hush refuses the exchange for a user the agent
isn't allowed to act for.
"""

import asyncio
import base64
import dataclasses
import hashlib
import json
import pathlib
import secrets
import time
from urllib.parse import urlencode

import httpx2

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
JWT_BEARER_ASSERTION = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
REFRESH_MARGIN = 60


class HushRefused(Exception):
    """Hush said no: to the sign-in, the exchange, or a refresh."""

    def __init__(self, step: str, status: int, error: str, description: str):
        super().__init__(f"{step}: {description or error} ({status})")
        self.step, self.status, self.error, self.description = step, status, error, description

    @property
    def not_assigned(self) -> bool:
        return "may not act for this user" in self.description


@dataclasses.dataclass
class Gateway:
    """What the gateway publishes about where its users and agents sign in."""

    mcp_url: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str

    @classmethod
    async def discover(cls, http: httpx2.AsyncClient, mcp_url: str) -> "Gateway":
        """The standard MCP way: the gateway names its authorization server,
        whose metadata names the endpoints."""
        base = mcp_url.removesuffix("/mcp")
        resource = (await http.get(f"{base}/.well-known/oauth-protected-resource/mcp")).raise_for_status().json()
        issuer = resource["authorization_servers"][0]
        server = (await http.get(f"{issuer}/.well-known/oauth-authorization-server")).raise_for_status().json()
        return cls(mcp_url, issuer, server["authorization_endpoint"], server["token_endpoint"],
                   server["registration_endpoint"])


@dataclasses.dataclass
class UserTokens:
    access: str
    refresh: str | None
    expires_at: float

    @property
    def claims(self) -> dict:
        return claims(self.access)


@dataclasses.dataclass
class Session:
    """One browser's sign-in for the agent: who it is, and the sign-ins it
    started, so a sign-in finishes only in the browser that began it."""

    user: UserTokens | None = None
    pending: dict[str, str] = dataclasses.field(default_factory=dict)
    refreshing: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)
    # The browser named a session the agent doesn't have: it restarted, and
    # keeps sessions in memory only.
    forgotten: bool = False


class Identity:
    """The agent's own identity, and signing users in for it."""

    def __init__(self, http: httpx2.AsyncClient, gateway_url: str, redirect_uri: str,
                 service_account_token: pathlib.Path):
        self.http = http
        self.gateway_url = gateway_url
        self.redirect_uri = redirect_uri
        self.service_account_token = service_account_token
        self._gateway: Gateway | None = None
        self._client_id: str | None = None

    async def gateway(self) -> Gateway:
        if self._gateway is None:
            self._gateway = await Gateway.discover(self.http, self.gateway_url)
        return self._gateway

    async def agent_resource(self) -> str:
        """This agent, as users sign in for it. Asked of Hush each time, not
        configured: registering the agent again gives it a new id."""
        try:
            body = await self._token("agent", {"grant_type": "client_credentials", **self._assertion()})
        except HushRefused as e:
            # Hush answers the same for an agent it doesn't know and for a
            # token issued for another audience.
            if e.error == "invalid_client":
                raise HushRefused("agent", e.status, e.error, "Hush didn't accept the agent's identity: is it "
                                  f"registered in Hush? ({e.description or e.error})") from e
            raise
        agent_id = claims(body["access_token"]).get("agent_id")
        if not agent_id:
            raise HushRefused("agent", 502, "unexpected_token", "Hush's token for the agent names no agent")
        # The gateway's issuer is <api>/deployments/<deployment>/agents; an
        # agent's resource is <api>/agents/<agent id>.
        issuer = (await self.gateway()).issuer
        api, found, _ = issuer.partition("/deployments/")
        if not found:
            raise HushRefused("agent", 502, "unexpected_issuer",
                              f"can't tell the agent's resource from the gateway's issuer {issuer}")
        return f"{api}/agents/{agent_id}"

    # --- The user signs in, for this agent ---

    async def sign_in_url(self, session: Session) -> str:
        gateway = await self.gateway()
        # Before keeping the sign-in: a refused one leaves no session behind.
        resource = await self.agent_resource()
        client_id = await self._client()
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(16)
        session.pending[state] = verifier
        query = urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
            # The user's token is for this agent only: no one else can use it.
            "resource": resource,
        })
        return f"{gateway.authorization_endpoint}?{query}"

    async def finish_sign_in(self, session: Session, state: str, code: str) -> None:
        verifier = session.pending.pop(state, None)
        if verifier is None:
            raise HushRefused("sign-in", 400, "invalid_request", "unknown or expired sign-in, start again")
        session.user = _user_tokens(await self._token("sign-in", {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": verifier,
            "client_id": await self._client(),
        }))

    async def user_token(self, session: Session) -> str:
        """The user's token, refreshed when it's about to expire: it lasts
        only a few minutes. One refresh at a time: a refresh token is good
        for one use."""
        async with session.refreshing:
            user = session.user
            if user is None:
                raise HushRefused("sign-in", 401, "login_required",
                                  "the agent restarted: sign in again" if session.forgotten
                                  else "sign in first")
            if time.time() <= user.expires_at - REFRESH_MARGIN:
                return user.access
            if not user.refresh:
                session.user = None
                raise HushRefused("sign-in", 401, "login_required", "your sign-in expired, sign in again")
            try:
                body = await self._token("refresh", {
                    "grant_type": "refresh_token",
                    "refresh_token": user.refresh,
                    "client_id": await self._client(),
                })
            except HushRefused:
                session.user = None
                raise
            session.user = _user_tokens(body, previous_refresh=user.refresh)
            return session.user.access

    # --- The agent exchanges the user's token and its own for one naming both ---

    async def exchange(self, session: Session) -> str:
        return (await self._token("exchange", {
            "grant_type": TOKEN_EXCHANGE,
            "subject_token": await self.user_token(session),
            "subject_token_type": ACCESS_TOKEN_TYPE,
            **self._assertion(),
        }))["access_token"]

    def _assertion(self) -> dict:
        return {
            "client_assertion_type": JWT_BEARER_ASSERTION,
            # Read fresh each time: the kubelet rotates it.
            "client_assertion": self.service_account_token.read_text().strip(),
        }

    async def _client(self) -> str:
        """This agent's sign-in page, registered with Hush as a public OAuth
        client (dynamic client registration). It carries no secret."""
        if self._client_id is None:
            gateway = await self.gateway()
            resp = await self.http.post(gateway.registration_endpoint, json={
                "client_name": "Hosted agent tutorial",
                "redirect_uris": [self.redirect_uri],
                "token_endpoint_auth_method": "none",
            })
            self._client_id = resp.raise_for_status().json()["client_id"]
        return self._client_id

    async def _token(self, step: str, form: dict) -> dict:
        gateway = await self.gateway()
        resp = await self.http.post(gateway.token_endpoint, data=form)
        if resp.status_code != 200:
            try:
                body = resp.json()
            except ValueError:
                body = {"error_description": resp.text}
            raise HushRefused(step, resp.status_code, body.get("error", ""), body.get("error_description", ""))
        return resp.json()


def claims(token: str) -> dict:
    """A token's claims, unverified: for showing, never for trusting."""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _user_tokens(body: dict, previous_refresh: str | None = None) -> UserTokens:
    return UserTokens(body["access_token"], body.get("refresh_token") or previous_refresh,
                      time.time() + body["expires_in"])
