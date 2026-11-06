"""Expenses' public address when a Cloudflare quick tunnel serves it: the
tunnel picks a random name when it starts, and reports it on its metrics port."""

import asyncio
import logging

import httpx2

log = logging.getLogger(__name__)


async def public_address(http: httpx2.AsyncClient, tunnel_url: str, attempts: int = 60, interval: float = 2) -> str:
    """https://<name>.trycloudflare.com, once the tunnel at tunnel_url has one."""
    for _ in range(attempts):
        try:
            resp = await http.get(f"{tunnel_url.rstrip('/')}/quicktunnel")
            hostname = resp.json().get("hostname") if resp.is_success else None
            if hostname:
                return f"https://{hostname}"
        except (httpx2.HTTPError, ValueError, AttributeError):
            pass
        log.info("waiting for the tunnel at %s", tunnel_url)
        await asyncio.sleep(interval)
    raise RuntimeError(f"the tunnel at {tunnel_url} reported no address")
