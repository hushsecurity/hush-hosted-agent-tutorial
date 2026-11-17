"""A stand-in for Hush's API, enough for the calls that put Expenses in Hush."""

import base64
import itertools
import json

import httpx2

API = "https://api.example.com"
KEY = ("key-id", "key-secret")


class FakeHushAPI:
    def __init__(self, page_size: int = 1):
        self.custom: dict[str, dict] = {}
        self.apps: dict[str, dict] = {}
        self.agents: dict[str, dict] = {}
        # Agent ids whose deletion Hush fails.
        self.broken_agents: set[str] = set()
        self.calls: list[str] = []
        self.page_size = page_size
        self.tokens = itertools.count(1)
        self.app_ids = itertools.count(1)
        self.token = None

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))

    def add_app(self, catalog_id: str, deployment: str, **fields) -> dict:
        app = {"id": f"app-{next(self.app_ids)}", "name": catalog_id, "app_catalog_id": catalog_id,
               "deployment_ids": [deployment], "assign_all": False, "assignments": [], **fields}
        self.apps[app["id"]] = app
        return app

    def add_agent(self, agent_id: str, *credentials: tuple[str, str], name: str | None = None) -> dict:
        """A service agent signing in with these (issuer, subject) credentials."""
        agent = {"id": agent_id, "subject_type": "service", "name": name or agent_id,
                 "credentials": [{"type": "kubernetes", "issuer": issuer, "subject": subject, "subject_match": "exact"}
                                 for issuer, subject in credentials]}
        self.agents[agent_id] = agent
        return agent

    def page(self, request: httpx2.Request, items: list) -> httpx2.Response:
        # Cursors with characters a URL must escape.
        cursor = request.url.params.get("cursor")
        start = int(cursor.removeprefix("p+").removesuffix("/=")) if cursor else 0
        end = start + self.page_size
        return httpx2.Response(200, json={"items": items[start:end],
                                          "next_page": f"p+{end}/=" if end < len(items) else None})

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content and path != "/v1/oauth/token" else None
        if path == "/v1/oauth/token":
            sent = request.headers.get("authorization", "")
            if sent != "Basic " + base64.b64encode(":".join(KEY).encode()).decode():
                return httpx2.Response(401, json={"error": "invalid_client"})
            self.token = f"token-{next(self.tokens)}"
            return httpx2.Response(200, json={"access_token": self.token})
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx2.Response(401)
        self.calls.append(f"{method} {path}")

        if path == "/v1/agents" and method == "GET":
            return self.page(request, [a for a in self.agents.values()
                                       if request.url.params.get("subject_type") in (None, a["subject_type"])])
        if path.startswith("/v1/agents/") and method == "DELETE":
            agent_id = path.split("/")[3]
            if agent_id in self.broken_agents:
                return httpx2.Response(500, json={"detail": "trouble"})
            if agent_id not in self.agents:
                return httpx2.Response(404, json={"detail": "Not Found"})
            del self.agents[agent_id]
            return httpx2.Response(204)
        if path == "/v1/applications/custom/mcp" and method == "POST":
            self.custom[body["name"]] = body
            return httpx2.Response(201, json={**body, "app_catalog_id": f"custom-{body['name']}"})
        if path.startswith("/v1/applications/custom/"):
            name = path.split("/")[4]
            if name not in self.custom:
                return httpx2.Response(404, json={"detail": "Not Found"})
            if method == "GET":
                return httpx2.Response(200, json=self.custom[name])
            if method == "PATCH":
                self.custom[name].update(body)
                return httpx2.Response(200, json=self.custom[name])
            if method == "DELETE":
                if any(a["app_catalog_id"] == f"custom-{name}" for a in self.apps.values()):
                    return httpx2.Response(409, json={"detail": "in use"})
                del self.custom[name]
                return httpx2.Response(204)
        if path == "/v1/applications" and method == "GET":
            return self.page(request, list(self.apps.values()))
        if path.startswith("/v1/applications/mcp/") and method == "POST":
            catalog_id = path.split("/")[4]
            if catalog_id.removeprefix("custom-") not in self.custom:
                return httpx2.Response(404)
            app = self.add_app(catalog_id, body["deployment_ids"][0], display_name=body["display_name"])
            return httpx2.Response(201, json=app)
        if path.startswith("/v1/applications/"):
            app = self.apps.get(path.split("/")[3])
            if app is None:
                return httpx2.Response(404)
            if method == "GET" and path.endswith("/mcp"):
                return httpx2.Response(200, json=app)
            if method == "PATCH" and path.endswith("/mcp"):
                app.update(body)
                return httpx2.Response(200, json=app)
            if method == "DELETE":
                del self.apps[app["id"]]
                return httpx2.Response(204)
        return httpx2.Response(405)
