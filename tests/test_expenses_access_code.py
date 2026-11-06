"""Expenses' optional access code, which keeps strangers out of a public
Expenses."""

from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from expenses import server
from expenses.store import Store
from tests.test_expenses_sign_in import BASE_URL, REDIRECT_URI

pytestmark = pytest.mark.anyio

CODE = "k7Qm-2xVa"


async def sign_in_page(access_code: str | None):
    """A client of Expenses at its sign-in page: the page, and how to submit it."""
    app = server.build(Store(), BASE_URL, access_code).streamable_http_app(host="0.0.0.0")
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE_URL)
    metadata = (await http.get("/.well-known/oauth-authorization-server")).json()
    client = (await http.post(metadata["registration_endpoint"], json={"redirect_uris": [REDIRECT_URI]})).json()
    authorize = await http.get(metadata["authorization_endpoint"], params={
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT_URI,
        "code_challenge": "x" * 43, "code_challenge_method": "S256",
    })
    page = urlsplit(authorize.headers["location"])
    request_id = parse_qs(page.query)["request"][0]
    shown = await http.get(f"{page.path}?{page.query}")

    async def submit(**fields):
        return await http.post(page.path, data={"request": request_id, "email": "dana@example.com", **fields})

    return shown, submit


async def test_without_a_code_the_page_asks_only_for_the_email():
    shown, submit = await sign_in_page(None)
    assert 'name="code"' not in shown.text
    assert (await submit()).status_code == 302


async def test_with_a_code_the_page_asks_for_it():
    shown, submit = await sign_in_page(CODE)
    assert 'name="code"' in shown.text
    resp = await submit(code=f" {CODE} ")
    assert resp.status_code == 302
    assert resp.headers["location"].startswith(REDIRECT_URI)


@pytest.mark.parametrize("fields", [{}, {"code": ""}, {"code": "wrong"}, {"code": CODE.lower()}])
async def test_a_missing_or_wrong_code_is_refused(fields):
    _, submit = await sign_in_page(CODE)
    resp = await submit(**fields)
    assert resp.status_code == 403
    assert "location" not in resp.headers
    assert 'value="dana@example.com"' in resp.text


async def test_the_configured_code_is_trimmed_like_the_typed_one():
    _, submit = await sign_in_page(f"{CODE}\n")
    assert (await submit(code=CODE)).status_code == 302


async def test_a_blank_code_means_no_code():
    shown, submit = await sign_in_page("   \n")
    assert 'name="code"' not in shown.text
    assert (await submit()).status_code == 302
