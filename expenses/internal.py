"""What Expenses saw, for the tutorial page to show.

Served on a port of its own that stays inside the cluster, never on the
app's public address.
"""

import dataclasses

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from expenses.store import Store


def build(store: Store) -> Starlette:
    async def calls(request: Request) -> JSONResponse:
        return JSONResponse([dataclasses.asdict(c) for c in reversed(store.calls)])

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    return Starlette(routes=[Route("/calls", calls), Route("/healthz", healthz)])
