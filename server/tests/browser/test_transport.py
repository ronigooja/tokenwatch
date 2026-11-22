import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tokenwatch.browser import AccountConfig
from tokenwatch.browser._proxy_worker import _serve
from tokenwatch.browser.transport import PProxyForwarder, _PlaywrightSession


class BrowserDomTests(unittest.IsolatedAsyncioTestCase):
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
