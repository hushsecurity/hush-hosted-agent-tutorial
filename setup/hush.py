"""Expenses in Hush: in the catalog, and on the gateway for all users.

Made through Hush's API with an API key, so the reader doesn't have to. Tools
are declared rather than detected: Hush's default policy for each comes from
its type, as it would from Detect tools in the Hush UI.
"""

import dataclasses
from typing import Any

import httpx2

from expenses import server
from expenses.store import Store

DISPLAY_NAME = "Expenses (tutorial)"
# What tells the tutorial's own catalog entry from one someone else made.
MARK = "Created by the Hush hosted agent tutorial."


class HushAPIError(Exception):
    def __init__(self, method: str, path: str, status: int, body: str):
        super().__init__(f"{method} {path}: {status} {body[:300]}")
        self.status = status


class NotOurs(Exception):
    def __init__(self, name: str):
        super().__init__(f"Hush already has an app named {name} that the tutorial didn't create: "
                         "set expenses.name to another name")


class Unmarked(Exception):
    def __init__(self, name: str):
        super().__init__(f"{name} is on the gateway, but its catalog entry doesn't carry the tutorial's mark")


def api_and_deployment(gateway_issuer: str) -> tuple[str, str]:
    """Hush's API address and the gateway's deployment, from the gateway's
    issuer: <api>/v1/deployments/<deployment>/agents."""
    api, found, rest = gateway_issuer.partition("/v1/deployments/")
    deployment = rest.split("/")[0]
    if not found or not deployment:
        raise ValueError(f"not a gateway issuer: {gateway_issuer}")
    return api, deployment


class HushAPI:
    def __init__(self, http: httpx2.AsyncClient, api_url: str, key_id: str, key_secret: str):
        self.http = http
        self.api_url = api_url.rstrip("/")
        self.key = (key_id, key_secret)
        self._token: str | None = None

    async def request(self, method: str, path: str, body: Any = None, params: dict | None = None) -> Any:
        if self._token is None:
            self._token = await self._sign_in()
        resp = await self._send(method, path, body, params)
        if resp.status_code == 401:
            self._token = await self._sign_in()
            resp = await self._send(method, path, body, params)
        if resp.status_code >= 400:
            raise HushAPIError(method, path, resp.status_code, resp.text)
        return resp.json() if resp.content else None

    async def _send(self, method: str, path: str, body: Any, params: dict | None) -> httpx2.Response:
        return await self.http.request(method, f"{self.api_url}{path}", json=body, params=params,
                                       headers={"Authorization": f"Bearer {self._token}"})

    async def _sign_in(self) -> str:
        resp = await self.http.post(f"{self.api_url}/v1/oauth/token", data={"grant_type": "client_credentials"},
                                    auth=self.key)
        if resp.status_code != 200:
            raise HushAPIError("POST", "/v1/oauth/token", resp.status_code, resp.text)
        return resp.json()["access_token"]


@dataclasses.dataclass
class Expenses:
    """Expenses as Hush should have it."""

    name: str  # in the catalog; the gateway's application comes from custom-<name>
    deployment: str  # the gateway's
    url: str = ""  # its public address: not needed to remove it

    @property
    def catalog_id(self) -> str:
        return f"custom-{self.name}"


async def tools(url: str) -> list[dict]:
    """Expenses' tools, typed from how each describes itself."""
    def kind(annotations) -> str:
        if annotations and annotations.read_only_hint:
            return "read"
        # MCP takes a tool that doesn't say otherwise as destructive.
        return "write" if annotations and annotations.destructive_hint is False else "destructive"

    listed = await server.build(Store(), url).list_tools()
    return [{"name": t.name, "type": kind(t.annotations), "description": t.description or ""} for t in listed]


async def put(api: HushAPI, expenses: Expenses) -> dict:
    """Creates Expenses in Hush, or brings it up to date. Returns the
    gateway's application."""
    custom = {
        "display_name": DISPLAY_NAME,
        "urls": [{"label": None, "url": f"{expenses.url}/mcp"}],
        "tools": await tools(expenses.url),
    }
    existing = await catalog_entry(api, expenses)
    if existing is None:
        # No auth of its own: each user signs in to Expenses.
        await api.request("POST", "/v1/applications/custom/mcp", {
            "name": expenses.name, "description": MARK, "scopes": [], "headers": {}, "oauth_relay": False, **custom,
        })
    elif existing.get("description") != MARK:
        raise NotOurs(expenses.name)
    else:
        # Updating the catalog entry updates the application too.
        await api.request("PATCH", f"/v1/applications/custom/{expenses.name}/mcp", custom)

    app = await application(api, expenses)
    if app is None:
        app = await api.request("POST", f"/v1/applications/mcp/{expenses.catalog_id}", {
            "display_name": DISPLAY_NAME, "deployment_ids": [expenses.deployment], "enabled": True,
        })
    # Assignments the reader added stay: only assign_all is set.
    return await api.request("PATCH", f"/v1/applications/{app['id']}/mcp", {"assign_all": True})


async def remove(api: HushAPI, expenses: Expenses) -> None:
    """Removes Expenses from the gateway, then from the catalog: only when the
    tutorial made it."""
    existing = await catalog_entry(api, expenses)
    if existing is None:
        return
    app = await application(api, expenses)
    if existing.get("description") != MARK:
        # On this gateway it's most likely ours, its mark edited: say so
        # rather than leave it behind quietly.
        raise Unmarked(expenses.name) if app is not None else NotOurs(expenses.name)
    if app is not None:
        await api.request("DELETE", f"/v1/applications/{app['id']}")
    await api.request("DELETE", f"/v1/applications/custom/{expenses.name}")


async def catalog_entry(api: HushAPI, expenses: Expenses) -> dict | None:
    try:
        return await api.request("GET", f"/v1/applications/custom/{expenses.name}/mcp")
    except HushAPIError as e:
        if e.status == 404:
            return None
        raise


async def application(api: HushAPI, expenses: Expenses) -> dict | None:
    cursor = None
    while True:
        params = {"type": "mcp", **({"cursor": cursor} if cursor else {})}
        page = await api.request("GET", "/v1/applications", params=params)
        for app in page.get("items", []):
            if app.get("app_catalog_id") == expenses.catalog_id and expenses.deployment in app.get("deployment_ids", []):
                return app
        cursor = page.get("next_page")
        if not cursor:
            return None
