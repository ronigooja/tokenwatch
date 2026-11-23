import asyncio
from types import SimpleNamespace

import pytest

from tokenwatch.browser.client import _BrowserFailure, _through
from tokenwatch.browser.transport import _PlaywrightSession
from tokenwatch.scheduler import Scheduler


class Forward:
    alive = True

    async def wait(self):
        await asyncio.Event().wait()


class Page:
    url = "https://arkm.com/login?token=hidden#fragment"

    def on(self, *args):
        pass

    async def title(self):
        return "Just a moment..."


class Context:
    def on(self, *args):
        pass


@pytest.mark.parametrize("message", [
    "Failed to fetch",
    "Failed https://example.com/path?token=hidden user:pass@host cookie=hidden token=hidden",
    "x" * 500,
])
def test_browser_failure_detail(message, caplog):
    async def run():
        async def fail():
            raise TypeError(message)

        session = _PlaywrightSession(None, Context(), Page())
        with pytest.raises(_BrowserFailure) as caught:
            await _through(Forward(), fail, session.failure_context)
        return caught.value

    exc = asyncio.run(run())
    exc.endpoint = "/user"
    last_error = Scheduler.log_failure(SimpleNamespace(id=1, kind="test"), exc)
    assert exc.detail.startswith("TypeError:")
    assert len(exc.detail) <= 120 and len(last_error) <= 200
    assert "Just a moment..." in exc.detail
    assert "https://arkm.com/login" in exc.detail
    for secret in ("example.com", "hidden", "user:pass@host", "fragment", "?token"):
        assert secret not in exc.detail + last_error + caplog.text
    assert "detail=" in caplog.text


def test_browser_failure_title_error():
    class BrokenPage(Page):
        async def title(self):
            raise RuntimeError("secret")

    async def run():
        async def fail():
            raise TypeError("Failed to fetch")

        session = _PlaywrightSession(None, Context(), BrokenPage())
        with pytest.raises(_BrowserFailure) as caught:
            await _through(Forward(), fail, session.failure_context)
        assert "TypeError: Failed to fetch" in caught.value.detail
        assert "secret" not in caught.value.detail

    asyncio.run(run())
