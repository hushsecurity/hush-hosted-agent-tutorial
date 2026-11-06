"""Runs Expenses: the MCP server on its public port, and the log of calls on
an internal one.

    python -m expenses
"""

import asyncio
import contextlib
import logging
import os
import signal

import uvicorn

from expenses import internal, server
from expenses.store import Store

PORT = int(os.getenv("EXPENSES_PORT", "8000"))
INTERNAL_PORT = int(os.getenv("EXPENSES_INTERNAL_PORT", "8001"))
# Clients keep MCP streams open: don't wait on them for long when stopping.
SHUTDOWN_GRACE = 5


class Server(uvicorn.Server):
    """A uvicorn server that leaves signals to main(), which stops both."""

    def capture_signals(self):
        return contextlib.nullcontext()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")
    store = Store()
    # host="0.0.0.0" also turns off the SDK's localhost-only Host check: in
    # the cluster, the gateway reaches Expenses by its public name.
    public = server.build(store).streamable_http_app(host="0.0.0.0")
    servers = [
        Server(uvicorn.Config(app, host="0.0.0.0", port=port, timeout_graceful_shutdown=SHUTDOWN_GRACE))
        for app, port in ((public, PORT), (internal.build(store), INTERNAL_PORT))
    ]

    def stop() -> None:
        for s in servers:
            s.force_exit = s.should_exit  # a second Ctrl+C stops at once
            s.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
