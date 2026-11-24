import asyncio
import traceback
import unittest
from collections import deque
from dataclasses import replace
from pathlib import Path

from tokenwatch.browser import AccountConfig, BrowserConfig, BrowserUpstreamClient
from tokenwatch.browser.client import _decode, _through, sign_payload
from tokenwatch.browser.config import proxy_parts
from tokenwatch.browser.transport import Response
from tokenwatch.upstream import AuthRequired, BadResponse, Blocked, RateLimited

TIMESTAMP = 1700000000
SIGNATURE = "685c717afc86f9c28a216b2bfd06d1009bd6e7fc064d8ad2d7302864d27b9169"
PROXY = "socks5h://mock-user:mock-password@proxy.example:1080"
CONFIG = BrowserConfig(Path("chrome"), Path("profiles"), "test-key", (
    AccountConfig("one", PROXY, 1), AccountConfig("two", PROXY, 2),
), request_timeout=60, close_timeout=1)


class FakeForward:
    port = 12345

    def __init__(self):
        self.exited = asyncio.Event()

    @property
    def alive(self):
        return not self.exited.is_set()

    async def wait(self):
        await self.exited.wait()

    async def close(self):
        self.exited.set()


class FakeForwarder:
    def __init__(self):
        self.started = []
        self.error = None

    async def start(self, account):
        if self.error:
            raise self.error
        forward = FakeForward()
        self.started.append(forward)
        return forward


class FakeSession:
    def __init__(self):
        self.started = asyncio.Event()
        self.resume = asyncio.Event()
        self.resume.set()
        self.closed = asyncio.Event()
        self.opened = False
        self.titles = deque(["Arkham"])
        self.fetches = []
        self.error = None
        self.pages = []

    async def open_site(self):
        self.opened = True

    async def login_page(self, page, email, password, timeout):
        self.pages.append((page, email, password, timeout))

    async def title(self):
        if len(self.titles) > 1:
            return self.titles.popleft()
        return self.titles[0]

    async def account_status(self):
        return "unknown"

    async def fetch(self, url, headers):
        self.fetches.append((url, headers))
        self.started.set()
        await self.resume.wait()
        if self.error:
            raise self.error
        return getattr(self, "response", Response(200, '[]'))

    async def wait_closed(self):
        await self.closed.wait()

    async def close(self):
        self.closed.set()


class FakeLauncher:
    def __init__(self, *sessions):
        self.sessions = deque(sessions)
        self.launches = []

    async def launch(self, binary, profile, account, port):
        self.launches.append((binary, profile, account, port))
        return self.sessions.popleft()


class ResponseTests(unittest.TestCase):
    def test_close_timeout_validation(self):
        for value in (0, -1, float('nan'), True):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "close_timeout"):
                replace(CONFIG, close_timeout=value)

    def test_request_timeout_validation(self):
        for value in (0, -1, float('nan'), True, 30, 1):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "request_timeout.*30-second"):
                replace(CONFIG, request_timeout=value)
        with self.assertRaises(TypeError):
            BrowserConfig(Path("chrome"), Path("profiles"), "key", ())

    def test_blocked_response_causes(self):
        for response, cause in ((Response(503, "", cf_mitigated="challenge"), "challenge_response"),
                                (Response(403, "Just a moment"), "challenge_403")):
            with self.assertRaises(Blocked) as caught:
                _decode(response, TIMESTAMP)
            self.assertEqual((caught.exception.cause, caught.exception.status), (cause, response.status))

    def test_screenshot_config_requires_retention(self):
        for keep in (None, 0, True, 1.5):
            with self.subTest(keep=keep), self.assertRaisesRegex(ValueError, "blocked_screenshot_keep"):
                replace(CONFIG, blocked_screenshot_dir=Path("private"), blocked_screenshot_keep=keep)
        replace(CONFIG, blocked_screenshot_keep=2)

    def test_documented_signature(self):
        self.assertEqual(sign_payload("/transfers", TIMESTAMP, "test-key"), SIGNATURE)

    def test_error_mapping(self):
        cases = (
            (Response(401, "oops"), AuthRequired),
            (Response(403, "transfers.explorer denied at anonymous"), AuthRequired),
            (Response(403, "<title>Just a moment...</title>"), Blocked),
            (Response(403, "", cf_mitigated="challenge"), Blocked),
            (Response(403, '{}'), BadResponse),
            (Response(503, '{}'), BadResponse),
            (Response(200, "not JSON"), BadResponse),
            (Response(200, "NaN"), BadResponse),
        )
        for response, expected in cases:
            with self.subTest(response=response), self.assertRaises(expected):
                _decode(response, TIMESTAMP)
        self.assertEqual(_decode(Response(200, '[1]'), TIMESTAMP), [1])

    def test_retry_after(self):
        for value, expected in (("42", 42), ("Tue, 14 Nov 2023 22:13:30 GMT", 10), ("invalid", None)):
            with self.subTest(value=value), self.assertRaises(RateLimited) as error:
                _decode(Response(429, "", value), TIMESTAMP)
            self.assertEqual(error.exception.retry_after, expected)

    def test_config_does_not_render_secrets(self):
        self.assertNotIn("mock-password", repr(CONFIG))
        self.assertNotIn("test-key", repr(CONFIG))
        self.assertEqual(proxy_parts("socks5h://mock:p%3Aa%23ss@[::1]:1080"), ("::1", 1080, "mock", "p:a#ss"))
        with self.assertRaises(ValueError) as error:
            proxy_parts("socks5h://mock:mock-password@host:invalid")
        self.assertNotIn("mock-password", str(error.exception))
        self.assertIsNone(error.exception.__context__)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_hung_close_is_bounded_and_relaunches(self):
        from unittest.mock import patch, AsyncMock
        from tokenwatch.browser.client import _BrowserFailure
        first, second = FakeSession(), FakeSession()
        first.resume.clear()
        async def close(): await asyncio.Future()
        first.close = close
        client, launcher, _ = self.client(first, second)
        object.__setattr__(client._config, "request_timeout", 0.02)
        object.__setattr__(client._config, "close_timeout", 0.02)
        with patch("tokenwatch.browser.transport.force_kill_browser", new_callable=AsyncMock) as kill:
            with self.assertRaises(_BrowserFailure):
                await asyncio.wait_for(client.get_json("one", "/user"), 1)
            kill.assert_awaited_once_with(Path("profiles/one"), first)
            self.assertNotIn("one", client._instances)
            self.assertFalse(client._locks["one"].locked())
            self.assertEqual(await client.get_json("one", "/user"), [])
            self.assertEqual(len(launcher.launches), 2)

    async def test_queue_excluded_from_deadline(self):
        first, second = FakeSession(), FakeSession()
        first.resume.clear()
        client, _, _ = self.client(first, second)
        object.__setattr__(client._config, "request_timeout", 0.04)
        async with client._locks["one"]:
            task = asyncio.create_task(client.get_json("one", "/user"))
            await asyncio.sleep(0.07)
            self.assertFalse(task.done())
        first.resume.set()
        self.assertEqual(await task, [])

    async def test_slot_queue_excluded_from_deadline(self):
        first, second = FakeSession(), FakeSession()
        first.resume.clear()
        client, _, _ = self.client(first, second)
        object.__setattr__(client._config, "request_timeout", 0.2)
        task1 = asyncio.create_task(client.get_json("one", "/user"))
        await first.started.wait()
        task2 = asyncio.create_task(client.get_json("two", "/user"))
        await asyncio.sleep(0.25)
        # The first request times out, releasing its slot; the second then runs.
        from tokenwatch.browser.client import _BrowserFailure
        with self.assertRaises(_BrowserFailure):
            await task1
        self.assertEqual(await task2, [])

    async def test_shutdown_hung_close_and_external_cancellation(self):
        from unittest.mock import patch, AsyncMock
        first = FakeSession()
        started = asyncio.Event()
        async def close():
            started.set()
            await asyncio.Future()
        first.close = close
        client, _, _ = self.client(first)
        object.__setattr__(client._config, "close_timeout", 0.02)
        await client.get_json("one", "/user")
        with patch("tokenwatch.browser.transport.force_kill_browser", new_callable=AsyncMock):
            await asyncio.wait_for(client.aclose(), 1)
        second = FakeSession()
        second.close = close
        client2, _, _ = self.client(second)
        await client2.get_json("one", "/user")
        started.clear()
        with patch("tokenwatch.browser.transport.force_kill_browser", new_callable=AsyncMock):
            task = asyncio.create_task(client2.aclose())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_start_hang_times_out(self):
        from tokenwatch.browser.client import _BrowserFailure
        session = FakeSession()
        async def hang(): await asyncio.Future()
        session.open_site = hang
        client, _, _ = self.client(session)
        object.__setattr__(client._config, "request_timeout", 0.02)
        with self.assertRaises(_BrowserFailure):
            await asyncio.wait_for(client.get_json("one", "/user"), 1)
        self.assertTrue(session.closed.is_set())

    async def test_force_kill_exact_profile(self):
        import signal
        from tempfile import TemporaryDirectory
        from tokenwatch.browser.transport import _profile_processes, force_kill_browser
        with TemporaryDirectory() as root:
            proc = Path(root)
            for pid, profile in ((123, "/profiles/one"), (124, "/profiles/two")):
                entry = proc / str(pid)
                entry.mkdir()
                (entry / "cmdline").write_bytes(f"chrome\0--user-data-dir={profile}\0".encode())
                (entry / "stat").write_text(f"{pid} (chrome) " + " ".join(["S"] + ["0"] * 18 + ["77"]))
            calls = []
            await force_kill_browser(Path("/profiles/one"), lister=lambda p: _profile_processes(p, proc),
                                     kill=lambda pid, sig: calls.append((pid, sig)))
            self.assertEqual(calls, [(123, signal.SIGTERM), (123, signal.SIGKILL)])
            self.assertEqual(_profile_processes(Path("/profiles/one"), proc / "missing"), [])

    async def test_timeout_drops_and_relaunches(self):
        from tokenwatch.browser.client import _BrowserFailure
        for phase in ("fetch", "settle"):
            with self.subTest(phase=phase):
                first, second = FakeSession(), FakeSession()
                if phase == "fetch":
                    first.resume.clear()
                else:
                    async def hang(_):
                        await asyncio.Future()
                    first.settle = hang
                client, launcher, forwarder = self.client(first, second)
                object.__setattr__(client._config, "request_timeout", 0.02)
                with self.assertRaisesRegex(_BrowserFailure, "Browser operation failed") as caught:
                    await asyncio.wait_for(client.get_json("one", "/user"), 1)
                self.assertIn("TimeoutError", caught.exception.detail)
                self.assertTrue(first.closed.is_set())
                self.assertFalse(forwarder.started[0].alive)
                self.assertEqual(await client.get_json("one", "/user"), [])
                self.assertEqual(len(launcher.launches), 2)
                await client.aclose()

    async def test_request_cancellation_propagates(self):
        session = FakeSession()
        session.resume.clear()
        client, _, _ = self.client(session)
        task = asyncio.create_task(client.get_json("one", "/user"))
        await session.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(session.closed.is_set())
        await client.aclose()

    async def test_forwarder_blocked_causes(self):
        forward = FakeForward()
        forward.exited.set()
        async def action():
            await asyncio.Future()
        with self.assertRaises(Blocked) as caught:
            await _through(forward, action)
        self.assertEqual(caught.exception.cause, "forwarder_stopped")
        forward = FakeForward()
        task = asyncio.create_task(_through(forward, action))
        await asyncio.sleep(0)
        forward.exited.set()
        with self.assertRaises(Blocked) as caught:
            await task
        self.assertEqual(caught.exception.cause, "forwarder_stopped")

        client, _, forwarder = self.client(FakeSession())
        forwarder.error = RuntimeError("failure")
        with self.assertRaises(Blocked) as caught:
            await client.get_json("one", "/user")
        self.assertEqual(caught.exception.cause, "forwarder_start_failed")
        await client.aclose()

    async def test_dead_lease_and_failed_proxy_causes(self):
        session = FakeSession()
        client, _, forwarder = self.client(session)
        await client.get_json("one", "/user")
        # Hold the condition so the watcher cannot remove the idle instance first.
        async with client._changed:
            forwarder.started[0].exited.set()
            task = asyncio.create_task(client.get_json("one", "/user"))
        with self.assertRaises(Blocked) as caught:
            await task
        self.assertEqual(caught.exception.cause, "forwarder_stopped")
        with self.assertRaises(Blocked) as caught:
            await client.get_json("one", "/user")
        self.assertEqual(caught.exception.cause, "forwarder_stopped")
        self.assertEqual(len(session.fetches), 1)
        await client.aclose()

    async def test_dead_forwarder_capture_failure_is_ignored(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            session = FakeSession()
            session.resume.clear()
            async def screenshot(path):
                Path(path).write_bytes(b"partial")
                raise RuntimeError("page closed")
            session.screenshot = screenshot
            client, _, forwarder = self.client(session, blocked_screenshot_dir=Path(directory), blocked_screenshot_keep=1)
            task = asyncio.create_task(client.get_json("one", "/user"))
            await asyncio.wait_for(session.started.wait(), 1)
            forwarder.started[0].exited.set()
            with self.assertRaises(Blocked) as caught:
                await task
            self.assertEqual(caught.exception.cause, "forwarder_stopped")
            self.assertFalse(hasattr(caught.exception, "screenshot"))
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertEqual(len(session.fetches), 1)
            await client.aclose()

    async def test_blocked_screenshot_and_rotation(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            session = FakeSession()
            session.response = Response(403, "Just a moment")
            async def screenshot(path):
                Path(path).write_bytes(b"fake png")
            session.screenshot = screenshot
            client, _, _ = self.client(session, blocked_screenshot_dir=Path(directory), blocked_screenshot_keep=1)
            with self.assertRaises(Blocked) as caught:
                await client.get_json("one", "/user")
            name = caught.exception.screenshot
            self.assertRegex(name, r"^\d+-[0-9a-f]{8}\.png$")
            self.assertEqual((Path(directory) / name).stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(directory).stat().st_mode & 0o777, 0o700)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
            await client.aclose()

    async def test_optional_settle_once_and_disabled(self):
        from unittest.mock import AsyncMock
        for seconds, count in ((15, 1), (0, 0)):
            session = FakeSession()
            session.settle = AsyncMock()
            client, _, _ = self.client(session, page_settle_seconds=seconds)
            await client.get_json("one", "/user")
            await client.get_json("one", "/user")
            self.assertEqual(session.settle.await_count, count)
            self.assertEqual(len(session.fetches), 2)
            await client.aclose()

    async def test_interactive_exempt_and_runtime_remove(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            login, other = FakeSession(), FakeSession()
            client, _, forwards = self.client(login, other, accounts_file=Path(directory) / "accounts.toml")
            await client.add_account(AccountConfig("three", PROXY, 3))
            task = asyncio.create_task(client.manual_login("one"))
            while not login.opened:
                await asyncio.sleep(0)
            self.assertEqual(await asyncio.wait_for(client.get_json("three", "/user"), 2), [])
            self.assertFalse(login.closed.is_set())
            await client.remove_account("three")
            self.assertTrue(other.closed.is_set())
            self.assertFalse(forwards.started[1].alive)
            login.closed.set()
            await task

    async def test_user_login_status(self):
        session = FakeSession()
        client, _, _ = self.client(session)
        for response, expected in ((Response(200, '{"id":"user"}'), "logged_in"),
                                   (Response(401, '{}'), "logged_out"),
                                   (Response(403, 'denied at anonymous'), "logged_out"),
                                   (Response(500, '{}'), "unknown"),
                                   (Response(200, 'not json'), "unknown")):
            session.response = response
            self.assertEqual(await client.account_status("one"), expected)

    async def test_replace_account_closes_instance(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            session = FakeSession()
            client, _, forwarder = self.client(session, accounts_file=Path(directory) / "accounts.toml",
                                                managed_ids=frozenset({"one"}))
            await client.get_json("one", "/user")
            updated = replace(CONFIG.accounts[0], password="secret")
            await client.replace_account(updated)
            self.assertTrue(session.closed.is_set())
            self.assertFalse(forwarder.started[0].alive)
            self.assertEqual(client.account_config("one"), (updated, True))

    async def test_manual_login_routes_credentials(self):
        session = FakeSession()
        account = replace(CONFIG.accounts[0], email="user@example.com", password="private")
        client, _, _ = self.client(session, accounts=(account, CONFIG.accounts[1]))
        task = asyncio.create_task(client.manual_login("one", page="register"))
        while not session.pages:
            await asyncio.sleep(0)
        self.assertEqual(session.pages, [("register", "user@example.com", "private", 60)])
        session.closed.set()
        await task

    def client(self, *sessions, **options):
        launcher, forwarder = FakeLauncher(*sessions), FakeForwarder()
        client = BrowserUpstreamClient(replace(CONFIG, **options), launcher=launcher,
                                       forwarder=forwarder, clock=lambda: TIMESTAMP)
        self.addAsyncCleanup(client.aclose)
        return client, launcher, forwarder

    async def test_request_starts_lazily_and_reuses_profile(self):
        session = FakeSession()
        client, launcher, _ = self.client(session)
        self.assertEqual(launcher.launches, [])
        self.assertEqual(await client.get_json("one", "/transfers", {"tokens": "a b"}), [])
        self.assertTrue(session.opened)
        self.assertEqual(await client.account_status("one"), "unknown")
        self.assertEqual(launcher.launches, [(Path("chrome"), Path("profiles/one"), CONFIG.accounts[0], 12345)])
        self.assertEqual(session.fetches[:1], [("https://api.arkm.com/transfers?tokens=a+b",
                                           {"X-Timestamp": str(TIMESTAMP), "X-Payload": SIGNATURE})])
        self.assertEqual(session.fetches[1][0], "https://api.arkm.com/user")
        self.assertEqual(session.fetches[1][1]["X-Payload"], sign_payload("/user", TIMESTAMP, CONFIG.signing_key))

    async def test_serializes_requests_for_one_account(self):
        session = FakeSession()
        session.resume.clear()
        client, launcher, _ = self.client(session)
        first = asyncio.create_task(client.get_json("one", "/transfers"))
        await asyncio.wait_for(session.started.wait(), 1)
        second = asyncio.create_task(client.get_json("one", "/transfers"))
        await asyncio.sleep(0)
        self.assertEqual(len(session.fetches), 1)
        session.resume.set()
        self.assertEqual(await asyncio.wait_for(asyncio.gather(first, second), 1), [[], []])
        self.assertEqual(len(session.fetches), 2)
        self.assertEqual(len(launcher.launches), 1)

    async def test_global_cap_replaces_idle_account(self):
        a, b = FakeSession(), FakeSession()
        a.resume.clear()
        client, launcher, _ = self.client(a, b)
        first = asyncio.create_task(client.get_json("one", "/transfers"))
        await asyncio.wait_for(a.started.wait(), 1)
        second = asyncio.create_task(client.get_json("two", "/transfers"))
        await asyncio.sleep(0)
        self.assertEqual(len(launcher.launches), 1)
        a.resume.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertTrue(a.closed.is_set())
        self.assertEqual(launcher.launches[-1][1], Path("profiles/two"))

    async def test_idle_closes_both_resources(self):
        session = FakeSession()
        client, _, forwarder = self.client(session, idle_timeout=0.01)
        await client.get_json("one", "/transfers")
        await asyncio.wait_for(session.closed.wait(), 1)
        await asyncio.wait_for(forwarder.started[0].wait(), 1)

    async def test_forwarder_exit_blocks_inflight_and_subsequent_requests(self):
        session = FakeSession()
        session.resume.clear()
        client, launcher, forwarder = self.client(session)
        request = asyncio.create_task(client.get_json("one", "/transfers"))
        await asyncio.wait_for(session.started.wait(), 1)
        forwarder.started[0].exited.set()
        with self.assertRaises(Blocked):
            await asyncio.wait_for(request, 1)
        with self.assertRaises(Blocked):
            await client.get_json("one", "/transfers")
        self.assertTrue(session.closed.is_set())
        self.assertEqual(len(launcher.launches), 1)

    async def test_credentials_are_not_in_failure_tracebacks(self):
        session = FakeSession()
        client, _, forwarder = self.client(session)
        forwarder.error = RuntimeError(PROXY)
        for expected in (Blocked, BadResponse):
            with self.assertRaises(expected) as error:
                await client.get_json("one", "/transfers")
            rendered = "".join(traceback.format_exception(error.exception))
            self.assertNotIn("mock-user", rendered)
            self.assertNotIn("mock-password", rendered)
            self.assertIsNone(error.exception.__context__)
            forwarder.error = None
            session.error = RuntimeError(PROXY)

    async def test_cf_waits_for_title_change_but_blocks_persistent_challenge(self):
        session, blocked = FakeSession(), FakeSession()
        session.titles = deque(["Just a moment...", "Arkham"])
        blocked.titles = deque(["Just a moment..."])
        client, _, _ = self.client(session, blocked, cf_wait_seconds=0.01)
        await client.get_json("one", "/transfers")
        with self.assertRaises(Blocked) as caught:
            await client.get_json("two", "/transfers")
        self.assertEqual(caught.exception.cause, "challenge_timeout")
        self.assertEqual(blocked.fetches, [])
        self.assertTrue(blocked.closed.is_set())

    async def test_manual_login_keeps_challenge_page_open_without_fetching(self):
        session = FakeSession()
        session.titles = deque(["Just a moment..."])
        client, _, _ = self.client(session, cf_wait_seconds=0, idle_timeout=0.01)
        ready = []
        login = asyncio.create_task(client.manual_login("one", ready=lambda: ready.append(session.opened)))
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(login), 0.03)
        self.assertTrue(session.opened)
        self.assertEqual(ready, [True])
        self.assertFalse(session.closed.is_set())
        self.assertEqual(session.fetches, [])
        session.closed.set()
        await asyncio.wait_for(login, 1)

    async def test_manual_login_does_not_signal_failed_launch(self):
        client, _, forwarder = self.client(FakeSession())
        forwarder.error = RuntimeError("failed")
        ready = []
        with self.assertRaises(Blocked):
            await client.manual_login("one", ready=lambda: ready.append(True))
        self.assertEqual(ready, [])
