"""Expenses taking its public address from a quick tunnel."""

import httpx2
import pytest

from expenses import tunnel

pytestmark = pytest.mark.anyio


def tunnel_reporting(*replies):
    """A tunnel whose metrics port gives these replies, one per request."""
    replies = list(replies)

    def handle(request):
        assert request.url.path == "/quicktunnel"
        reply = replies.pop(0)
        return reply if isinstance(reply, httpx2.Response) else httpx2.Response(200, json=reply)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))


async def test_waits_until_the_tunnel_has_a_name():
    http = tunnel_reporting(httpx2.Response(503), {"hostname": ""}, httpx2.Response(200, text="?"),
                            {"hostname": "calm-river.trycloudflare.com"})
    assert await tunnel.public_address(http, "http://tunnel:2000/", interval=0) == "https://calm-river.trycloudflare.com"


async def test_gives_up_when_the_tunnel_never_has_one():
    http = tunnel_reporting(*[{"hostname": ""}] * 3)
    with pytest.raises(RuntimeError):
        await tunnel.public_address(http, "http://tunnel:2000", attempts=3, interval=0)
