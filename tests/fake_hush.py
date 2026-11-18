"""A stand-in for the gateway and Hush's token endpoint, enough to test the
agent's side of the sign-in and the exchange."""

import base64
import json
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

GATEWAY = "https://gateway.example.com"
ISSUER = "https://hush.example.com/v1/deployments/dep-1/agents"
AGENT_ID = "agt-1"
AGENT_NAME = "expenses-assistant"
AGENT_RESOURCE = f"https://hush.example.com/v1/agents/{AGENT_ID}"
ALLOWED = {"dana@example.com"}


def jwt(claims: dict) -> str:
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


CLUSTER_ISSUER = "https://oidc.cluster.example.com"
SERVICE_ACCOUNT = "system:serviceaccount:tutorial:expenses-assistant"
SERVICE_ACCOUNT_TOKEN = jwt({"iss": CLUSTER_ISSUER, "sub": SERVICE_ACCOUNT, "aud": [ISSUER]})


def disabled_answer() -> JSONResponse:
    return JSONResponse({"error": "invalid_grant", "error_description": "agent is disabled"}, status_code=400)


class FakeHush:
    def __init__(self, user_email: str = "dana@example.com", user_ttl: int = 300,
                 registered: bool | Callable[[], bool] = True, disabled: bool = False):
        self.registered = registered
        self.disabled = disabled
        self.user_email = user_email
        self.user_ttl = user_ttl
        self.requests: list[dict] = []

    def app(self) -> Starlette:
        async def protected_resource(request: Request):
            return JSONResponse({"resource": f"{GATEWAY}/mcp", "authorization_servers": [ISSUER]})

        async def authorization_server(request: Request):
            return JSONResponse({
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token",
                "registration_endpoint": f"{ISSUER}/register",
            })

        async def register(request: Request):
            return JSONResponse({"client_id": "page-client", **await request.json()}, status_code=201)

        async def token(request: Request):
            form = dict(await request.form())
            self.requests.append(form)
            user = {"sub": "user-1", "email": self.user_email, "exp": int(time.time()) + self.user_ttl}
            grant = form["grant_type"]
            if grant == "client_credentials":
                registered = self.registered() if callable(self.registered) else self.registered
                if not registered or form.get("client_assertion") != SERVICE_ACCOUNT_TOKEN:
                    return JSONResponse({"error": "invalid_client", "error_description": "invalid assertion"}, status_code=401)
                if self.disabled:
                    return disabled_answer()
                return JSONResponse({"access_token": jwt({"sub": AGENT_ID, "agent_id": AGENT_ID, "agent_name": AGENT_NAME}),
                                     "expires_in": self.user_ttl})
            if grant == "refresh_token" and (self.disabled or self.user_email not in ALLOWED):
                return JSONResponse({"error": "invalid_grant",
                                     "error_description": "this agent may not act for this user"}, status_code=400)
            if grant in ("authorization_code", "refresh_token"):
                return JSONResponse({"access_token": jwt({**user, "aud": AGENT_RESOURCE}),
                                     "refresh_token": "refresh-1", "expires_in": self.user_ttl})
            if grant == "urn:ietf:params:oauth:grant-type:token-exchange":
                if form.get("client_assertion") != SERVICE_ACCOUNT_TOKEN:
                    return JSONResponse({"error": "invalid_client", "error_description": "invalid assertion"}, status_code=401)
                if self.disabled:
                    return disabled_answer()
                if self.user_email not in ALLOWED:
                    return JSONResponse({"error": "invalid_grant",
                                         "error_description": "this agent may not act for this user"},
                                        status_code=400)
                return JSONResponse({"access_token": jwt({**user, "act": {"sub": "agent-1"}}),
                                     "expires_in": self.user_ttl})
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

        return Starlette(routes=[
            Route("/.well-known/oauth-protected-resource/mcp", protected_resource),
            Route(f"{urlsplit(ISSUER).path}/.well-known/oauth-authorization-server", authorization_server),
            Route(f"{urlsplit(ISSUER).path}/register", register, methods=["POST"]),
            Route(f"{urlsplit(ISSUER).path}/token", token, methods=["POST"]),
        ])
