import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tokenwatch.browser.client import BrowserUpstreamClient
from tokenwatch.managed_login_navigation import capture, operate, valid_url


@pytest.mark.parametrize("url,valid", [
    ("https://example.org/path?q=secret", True), ("http://localhost:123/", True),
    ("javascript:alert(1)", False), ("https://user@example.org", False),
    ("https://example.org\\@evil.test", False), ("https://example.org\n", False),
    ("https:///path", False), ("http://example.org:abc", False),
])
def test_url_validation(url, valid):
    assert valid_url(url) is valid


def test_navigation_captures_page_and_does_not_block_location():
    asyncio.run(check_navigation())


async def check_navigation():
    entered, release = asyncio.Event(), asyncio.Event()

    class Page:
        async def call(self, op, **kwargs):
            entered.set()
            await release.wait()

        async def info(self):
            return {"url": "https://example.org/", "generation": 1, "title": ""}

    page = Page()
    upstream = SimpleNamespace(_login_pages={"a": page}, _login_autofill={},
                               _config=SimpleNamespace(close_timeout=2))
    session = SimpleNamespace(account_id="a", state="open", ticket="secret")
    logins = SimpleNamespace(manager_mode=True, current=session, upstream=upstream,
                             valid=lambda ticket: ticket == "secret" and logins.current is session)
    assert capture(logins, "secret") == (session, page)
    task = asyncio.create_task(operate(logins, "secret", session, page, "https://example.org/"))
    await entered.wait()
    assert (await operate(logins, "secret", session, page)).status_code == 200
    upstream._login_pages["a"] = Page()
    release.set()
    with pytest.raises(HTTPException) as error:
        await task
    assert error.value.status_code == 404


@pytest.mark.parametrize("timeout", [False, True])
def test_navigation_waits_for_inflight_autofill_without_cancelling(timeout):
    asyncio.run(check_inflight_autofill(timeout))


async def check_inflight_autofill(timeout):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Page:
        async def fill_credentials(self, *args):
            calls.append("fill")
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                calls.append("cancelled")
                raise
            return False

        async def call(self, op, **kwargs):
            calls.append(op)

        async def info(self):
            return {"url": "https://example.org/", "generation": 1, "title": ""}

    page = Page()
    stop = asyncio.Event()
    client = SimpleNamespace(_autofill_timeout=10)
    task = asyncio.create_task(BrowserUpstreamClient._manager_autofill(client, page, "email", "password", stop))
    await entered.wait()
    upstream = SimpleNamespace(_login_pages={"a": page}, _login_autofill={"a": (stop, task)},
                               _config=SimpleNamespace(close_timeout=0.05 if timeout else 2))
    session = SimpleNamespace(account_id="a", state="open", ticket="secret")
    logins = SimpleNamespace(manager_mode=True, current=session, upstream=upstream,
                             valid=lambda ticket: ticket == "secret" and logins.current is session)
    navigation = asyncio.create_task(operate(logins, "secret", session, page, "https://example.org/"))
    await asyncio.sleep(0)
    assert stop.is_set() and calls == ["fill"]
    if timeout:
        with pytest.raises(HTTPException) as error:
            await navigation
        assert error.value.status_code == 502
        assert not task.done()
    release.set()
    if not timeout:
        assert (await navigation).status_code == 200
    await task
    assert calls == (["fill"] if timeout else ["fill", "navigate"])
