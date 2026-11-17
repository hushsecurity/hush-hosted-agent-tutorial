"""What Expenses saw, and where it is, for the tutorial page to show; and a
reset, for starting the tutorial over.

Served on a port of its own that stays inside the cluster, never on the
app's public address.
"""

import dataclasses

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from expenses.store import Store


def build(store: Store, public_url: str, access_code: str | None = None) -> Starlette:
    async def calls(request: Request) -> JSONResponse:
        return JSONResponse([dataclasses.asdict(c) for c in reversed(store.calls)])

    async def about(request: Request) -> JSONResponse:
        # The public address may come from a tunnel, known only once Expenses runs.
        return JSONResponse({"url": public_url, "access_code": access_code})

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def reset(request: Request) -> JSONResponse:
        store.reset()
        return JSONResponse({"ok": True})

    return Starlette(routes=[Route("/calls", calls), Route("/about", about), Route("/healthz", healthz),
                             Route("/reset", reset, methods=["POST"])])
