import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tokenwatch.browser import AccountConfig
from tokenwatch.browser._proxy_worker import _serve
from tokenwatch.browser.transport import PProxyForwarder, _PlaywrightSession, persona_args


class PersonaTests(unittest.TestCase):
    def test_none_persona_adds_no_fingerprint_flags(self):
        account = AccountConfig("a1", "socks5h://u:p@proxy.example:1080", 1)
        self.assertEqual(account.persona, "none")
        self.assertEqual(persona_args(account), [])

    def test_windows_persona_adds_fingerprint_flags(self):
        account = AccountConfig("a1", "socks5h://u:p@proxy.example:1080", 7, persona="windows")
        args = persona_args(account)
        self.assertIn("--fingerprint=7", args)
        self.assertIn("--fingerprint-platform=windows", args)

    def test_windows_persona_disables_canvas_spoofing_by_default(self):
        account = AccountConfig("a1", "socks5h://u:p@proxy.example:1080", 7, persona="windows")
        self.assertEqual(persona_args(account)[-1], "--disable-spoofing=canvas")

    def test_disable_spoofing_is_configurable(self):
        base = ("a1", "socks5h://u:p@proxy.example:1080", 7)
        both = AccountConfig(*base, persona="windows", disable_spoofing=["canvas", "gpu"])
        self.assertEqual(persona_args(both)[-1], "--disable-spoofing=canvas,gpu")
        none = AccountConfig(*base, persona="windows", disable_spoofing=[])
        self.assertFalse(any(arg.startswith("--disable-spoofing") for arg in persona_args(none)))

    def test_invalid_disable_spoofing_is_rejected(self):
        for value in (["webgl"], ["canvas", "canvas"]):
            with self.assertRaises(ValueError):
                AccountConfig("a1", "socks5h://u:p@proxy.example:1080", 1, disable_spoofing=value)

    def test_unknown_persona_is_rejected(self):
        with self.assertRaises(ValueError):
            AccountConfig("a1", "socks5h://u:p@proxy.example:1080", 1, persona="mac")


class BrowserDomTests(unittest.IsolatedAsyncioTestCase):
    async def test_challenge_mouse_click_bounds(self):
        import random
        page = MagicMock(evaluate=AsyncMock(return_value={"width": 800, "height": 600}))
        page.mouse.move, page.mouse.down, page.mouse.up = AsyncMock(), AsyncMock(), AsyncMock()
        session = _PlaywrightSession(MagicMock(), MagicMock(), page, rng=random.Random(1))
        with patch("tokenwatch.browser.transport.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await session.click_challenge({"x": 10, "y": 20, "width": 300, "height": 60}, 1)
        x, y = page.mouse.move.await_args.args
        self.assertTrue(10 <= x < 310 and 20 <= y < 80 and x < 800 and y < 600)
        self.assertTrue(8 <= page.mouse.move.await_args.kwargs["steps"] <= 12)
        self.assertTrue(0.1 <= sleep.await_args.args[0] <= 0.2)
        page.mouse.down.assert_awaited_once()
        page.mouse.up.assert_awaited_once()

    async def test_failure_text_only_in_challenge_context(self):
        frame = MagicMock(url="https://challenges.cloudflare.com/widget")
        frame.frame_element = AsyncMock(return_value=MagicMock(bounding_box=AsyncMock(return_value=None)))
        frame.get_by_text.return_value.count = AsyncMock(return_value=0)
        page = MagicMock(title=AsyncMock(return_value="Just a moment..."), frames=[frame],
                         evaluate=AsyncMock(return_value={"width": 800, "height": 600}))
        stage = MagicMock()
        stage.get_by_text.return_value.count = AsyncMock(return_value=0)
        page.locator.side_effect = lambda selector: stage if selector == "#challenge-stage" else MagicMock(count=AsyncMock(return_value=1))
        page.get_by_text.return_value.count = AsyncMock(return_value=1)  # unrelated page text
        session = _PlaywrightSession(MagicMock(), MagicMock(), page)
        self.assertFalse((await session.challenge_info())["failure"])
        page.get_by_text.assert_not_called()
        frame.get_by_text.return_value.count.side_effect = RuntimeError("detached")
        stage.get_by_text.return_value.count.return_value = 1
        self.assertTrue((await session.challenge_info())["failure"])
        stage.get_by_text.return_value.count.side_effect = RuntimeError("detached")
        self.assertFalse((await session.challenge_info())["failure"])
    async def test_probe_recovery_never_repeats_fetch(self):
        page = MagicMock()
        page.wait_for_load_state = AsyncMock()
        page.evaluate = AsyncMock(side_effect=[RuntimeError("Execution context was destroyed"),
                                                "https://arkm.com", {"status": 200, "body": "[]"}])
        session = _PlaywrightSession(MagicMock(), MagicMock(), page)
        await session.fetch("https://api.arkm.com/user", {})
        self.assertEqual(page.evaluate.await_count, 3)
        self.assertEqual(sum(len(call.args) == 2 for call in page.evaluate.await_args_list), 1)
        page.wait_for_load_state.assert_awaited_once_with("load", timeout=15000)

    async def test_failed_probes_and_fetch_are_not_retried(self):
        for effects, expected_fetches in (([RuntimeError("Execution context was destroyed")] * 2, 0),
                                          (["origin", RuntimeError("Execution context was destroyed")], 1),
                                          ([RuntimeError("Target page, context or browser has been closed")], 0)):
            page = MagicMock(evaluate=AsyncMock(side_effect=effects), wait_for_load_state=AsyncMock())
            session = _PlaywrightSession(MagicMock(), MagicMock(), page)
            with self.assertRaises(RuntimeError):
                await session.fetch("https://api.arkm.com/user", {})
            self.assertEqual(sum(len(call.args) == 2 for call in page.evaluate.await_args_list), expected_fetches)

    async def test_settle_tolerates_networkidle_timeout_and_checks_page(self):
        page = MagicMock(url="https://arkm.com/", title=AsyncMock(return_value=""))
        page.wait_for_load_state = AsyncMock(side_effect=[None, TimeoutError()])
        session = _PlaywrightSession(MagicMock(), MagicMock(), page)
        await session.settle(2)
        self.assertEqual([c.args[0] for c in page.wait_for_load_state.await_args_list], ["load", "networkidle"])
        page.url = "https://example.invalid/"
        page.wait_for_load_state = AsyncMock()
        with self.assertRaises(RuntimeError):
            await session.settle(2)

    async def test_only_visible_actions_indicate_login_state(self):
        page = MagicMock(url="https://arkm.com/")
        session = _PlaywrightSession(MagicMock(), MagicMock(), page)
        visible = MagicMock(is_visible=AsyncMock(return_value=True))
        hidden = MagicMock(is_visible=AsyncMock(return_value=False))
        controls = {("button", "Log in"): [hidden]}
        page.get_by_role.side_effect = lambda role, *, name, exact: MagicMock(
            all=AsyncMock(return_value=controls.get((role, name), [])),
        )
        self.assertEqual(await session.account_status(), "unknown")
        controls[("button", "Log in")] = [visible]
        self.assertEqual(await session.account_status(), "logged_out")
        controls[("button", "Log in")] = []
        controls[("link", "Sign out")] = [visible]
        self.assertEqual(await session.account_status(), "logged_in")
        page.url = "https://example.invalid/"
        self.assertEqual(await session.account_status(), "unknown")

    async def test_fetch_passes_only_url_and_headers_to_page(self):
        page = MagicMock(evaluate=AsyncMock(return_value={"status": 200, "body": "[]"}))
        session = _PlaywrightSession(MagicMock(), MagicMock(), page)
        url = "https://api.arkm.com/transfers"
        headers = {"X-Timestamp": "1700000000", "X-Payload": "test-signature"}
        self.assertEqual((await session.fetch(url, headers)).body, "[]")
        expression, arg = page.evaluate.await_args.args
        self.assertIn("credentials: 'include'", expression)
        self.assertIn("redirect: 'error'", expression)
        self.assertEqual(arg, {"url": url, "headers": headers})


class ForwarderTests(unittest.IsolatedAsyncioTestCase):
    async def test_credentials_go_to_stdin_and_worker_selects_port(self):
        process = MagicMock(returncode=None)
        process.stdin.drain = AsyncMock()
        process.stdout.readline = AsyncMock(return_value=b"23456\n")
        start = AsyncMock(return_value=process)
        account = AccountConfig("one", "socks5h://fake-user:fake-password@proxy.example:1080", 1)
        with patch("tokenwatch.browser.transport.asyncio.create_subprocess_exec", start):
            forward = await PProxyForwarder().start(account)
        self.assertEqual(forward.port, 23456)
        self.assertNotIn("fake-password", repr(start.await_args.args))
        self.assertIn(b"fake-password", process.stdin.write.call_args.args[0])

    async def test_worker_replaces_pproxy_direct_fallback(self):
        server = MagicMock()
        upstream, listener = MagicMock(), MagicMock()
        server.proxy_by_uri.side_effect = [upstream, listener]
        bound = MagicMock()
        bound.sockets[0].getsockname.return_value = ("127.0.0.1", 23456)
        listener.start_server = AsyncMock()
        listener.start_server.return_value.__aenter__.return_value = bound
        stdin = MagicMock()
        stdin.buffer.readline.return_value = b'["proxy.example", 1080, "user", "pass"]\n'
        loop = asyncio.get_running_loop()
        with (
            patch.dict("sys.modules", {"pproxy": MagicMock(server=server)}),
            patch("tokenwatch.browser._proxy_worker.sys.stdin", stdin),
            patch("tokenwatch.browser._proxy_worker.os.read", return_value=b""),
            patch.object(loop, "add_reader", side_effect=lambda _, ready: ready()),
            patch("builtins.print") as announce,
        ):
            await _serve()
        announce.assert_called_once_with(23456, flush=True)
        self.assertEqual(listener.port, 0)
        self.assertEqual(upstream.users, [b"user:pass"])
        listener.start_server.assert_awaited_once_with({"rserver": [upstream]})
        with self.assertRaises(ConnectionError):
            await server.DIRECT.open_connection("example.invalid", 443)


def test_login_page_autofill_and_timeout():
    import asyncio
    from tokenwatch.browser.transport import _PlaywrightSession, REGISTER_URL

    class Field:
        def __init__(self, visible=True):
            self.visible = visible
            self.filled = []
            self.first = self
        async def wait_for(self, **kwargs):
            if not self.visible:
                await asyncio.Event().wait()
        async def fill(self, value, **kwargs):
            self.filled.append(value)

    class Page:
        def __init__(self, visible=True):
            self.email, self.password = Field(visible), Field(visible)
            self.url = None
        def on(self, *args):
            pass
        async def goto(self, url, **kwargs):
            self.url = url
        def locator(self, selector):
            return self.email if "email" in selector else self.password

    class Context:
        def on(self, *args):
            pass

    async def run():
        page = Page()
        session = _PlaywrightSession(None, Context(), page)
        await session.login_page("register", "secret-email", "secret-password", 0.01)
        assert page.url == REGISTER_URL
        assert page.email.filled == ["secret-email"] and page.password.filled == ["secret-password"]
        page = Page(False)
        session = _PlaywrightSession(None, Context(), page)
        await session.login_page("login", "secret-email", "secret-password", 0.001)
        assert not page.email.filled and not page.password.filled
        await session.login_page("login", None, None, 0.001)
        assert not page.email.filled
    asyncio.run(run())
