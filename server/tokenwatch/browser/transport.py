import asyncio
import json
import os
import random
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .config import AccountConfig, proxy_parts

SITE_URL = "https://arkm.com/"
LOGIN_URL = "https://arkm.com/login?redirectDomain=https://arkm.com"
REGISTER_URL = "https://arkm.com/register?redirectDomain=https://arkm.com"
API_ORIGIN = "https://api.arkm.com"


def _profile_processes(profile: Path, proc: Path = Path("/proc")):
    """Return PID/start-time pairs only for exact profile arguments on Linux."""
    result = []
    marker = f"--user-data-dir={profile.resolve()}".encode()
    try:
        entries = list(proc.iterdir())
    except OSError:
        return result
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
            if marker in args:
                stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                result.append((int(entry.name), stat[19]))
        except (OSError, IndexError, ValueError):
            continue
    return result


async def force_kill_browser(profile: Path, session=None, *, lister=_profile_processes, kill=os.kill):
    """Best-effort exact-profile cleanup; recheck identity before each signal."""
    try:
        targets = set(lister(profile))
        for sig in (signal.SIGTERM, signal.SIGKILL):
            current = set(lister(profile))
            for pid, started in targets & current:
                with suppress(OSError, ValueError):
                    kill(pid, sig)
            if sig == signal.SIGTERM and targets:
                await asyncio.sleep(0.05)
        # Playwright's private transport may expose its own driver process.
        driver = getattr(session, "_driver", None)
        connection = getattr(getattr(driver, "_impl_obj", None), "_connection", None)
        process = getattr(getattr(connection, "_transport", None), "_proc", None)
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError, OSError):
                process.kill()
            with suppress(Exception):
                await asyncio.wait_for(process.wait(), 0.1)
    except Exception:
        pass


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: str
    retry_after: str | None = None
    cf_mitigated: str | None = None


class BrowserSession(Protocol):
    async def open_site(self) -> None: ...
    async def login_page(self, page: str, email: str | None, password: str | None, timeout: float) -> None: ...
    async def title(self) -> str: ...
    async def account_status(self) -> str: ...
    async def fetch(self, url: str, headers: dict[str, str]) -> Response: ...
    async def wait_closed(self) -> None: ...
    async def close(self) -> None: ...


class Launcher(Protocol):
    async def launch(
        self, binary: Path, profile: Path, account: AccountConfig, proxy_port: int
    ) -> BrowserSession: ...


class Forward(Protocol):
    @property
    def port(self) -> int: ...
    @property
    def alive(self) -> bool: ...
    async def wait(self) -> None: ...
    async def close(self) -> None: ...


class Forwarder(Protocol):
    async def start(self, account: AccountConfig) -> Forward: ...


class _PlaywrightSession:
    def __init__(self, driver, context, page, rng=None):
        self._driver = driver
        self._context = context
        self._page = page
        self._rng = rng if rng is not None else random.Random()
        self._page_settle_seconds = 15
        self._closed = asyncio.Event()
        context.on("close", lambda *_: self._closed.set())
        page.on("close", lambda *_: self._closed.set())

    async def open_site(self) -> None:
        await self._page.goto(SITE_URL, wait_until="domcontentloaded")

    def configure_page_settle(self, timeout: float) -> None:
        self._page_settle_seconds = timeout

    async def settle(self, timeout: float) -> None:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        await self._page.wait_for_load_state("load", timeout=timeout * 1000)
        try:
            await self._page.wait_for_load_state("networkidle", timeout=timeout * 1000)
        except (PlaywrightTimeoutError, TimeoutError):
            pass
        if not (await self._page.title()).strip():
            parts = urlsplit(self._page.url)
            if (parts.scheme, parts.netloc) != ("https", "arkm.com"):
                raise RuntimeError("Page has no title and unexpected origin")

    async def login_page(self, page: str, email: str | None, password: str | None, timeout: float) -> None:
        try:
            await self._page.goto(LOGIN_URL if page == "login" else REGISTER_URL, wait_until="domcontentloaded")
            if email is None or password is None:
                return
            async with asyncio.timeout(timeout):
                email_field = self._page.locator("input[type=email], input[name=email]").first
                password_field = self._page.locator("input[type=password]").first
                await email_field.wait_for(state="visible", timeout=0)
                await password_field.wait_for(state="visible", timeout=0)
                await email_field.fill(email, timeout=0)
                await password_field.fill(password, timeout=0)
        except Exception:
            # Browser errors can contain field values; never propagate them.
            return

    async def title(self) -> str:
        return await self._page.title()

    async def challenge_info(self) -> dict:
        title = (await self._page.title()).lower()
        boxes = []
        for frame in self._page.frames:
            if urlsplit(frame.url).hostname != "challenges.cloudflare.com":
                continue
            element = await frame.frame_element()
            box = await element.bounding_box()
            if box and await element.is_visible():
                boxes.append(box)
        markers = await self._page.locator('#challenge-stage, [id*="cf_chl"], [class*="cf_chl"]').count()
        failure = await self._page.get_by_text("verification failed", exact=False).count()
        viewport = await self._page.evaluate("() => ({width: innerWidth, height: innerHeight})")
        return {"challenge": "just a moment" in title or bool(boxes) or bool(markers),
                "boxes": boxes, "viewport": viewport, "failure": bool(failure)}

    async def click_challenge(self, box, attempt) -> None:
        viewport = await self._page.evaluate("() => ({width: innerWidth, height: innerHeight})")
        x = min(box["x"] + box["width"] - 1, viewport["width"] - 1, box["x"] + self._rng.uniform(26, 28))
        y = min(box["y"] + box["height"] - 1, viewport["height"] - 1, box["y"] + self._rng.uniform(25, 27))
        await self._page.mouse.move(x, y, steps=self._rng.randint(8, 12))
        await self._page.mouse.down()
        try:
            await asyncio.sleep(self._rng.uniform(0.1, 0.2))
        finally:
            await self._page.mouse.up()

    async def screenshot(self, path: str) -> None:
        await self._page.screenshot(path=path, full_page=False, timeout=5000)

    async def failure_context(self) -> str:
        try:
            parts = urlsplit(self._page.url)
            # Never include userinfo, query or fragment.
            origin = f"{parts.scheme}://{parts.hostname}" if parts.scheme in ("http", "https") and parts.hostname else ""
            if origin and parts.port:
                origin += f":{parts.port}"
            path = parts.path if origin else ""
            # Page paths may contain account-specific identifiers; retain only known static paths.
            if path not in ("", "/", "/login", "/register", "/cdn-cgi/challenge-platform/"):
                path = "/<redacted>"
        except Exception:
            origin, path = "", ""
        try:
            title = await self._page.title()
            # Only the known challenge title is safe to expose from arbitrary pages.
            title = "Just a moment..." if "just a moment" in title.lower() else ""
        except Exception:
            title = ""
        return f"page={origin}{path} title={title[:60]}" if origin or title else ""

    async def account_status(self) -> str:
        if not self._page.url.startswith(SITE_URL):
            return "unknown"
        for name, status in (("Log in", "logged_out"), ("Log out", "logged_in"), ("Sign out", "logged_in")):
            for role in ("button", "link"):
                candidates = self._page.get_by_role(role, name=name, exact=True)
                for candidate in await candidates.all():
                    if await candidate.is_visible():
                        return status
        return "unknown"

    async def fetch(self, url: str, headers: dict[str, str]) -> Response:
        try:
            await self._page.evaluate("() => location.origin")
        except Exception as error:
            if ("Execution context was destroyed" not in str(error)
                    or "Target page, context or browser has been closed" in str(error)):
                raise
            if self._page_settle_seconds:
                await self._page.wait_for_load_state("load", timeout=self._page_settle_seconds * 1000)
            await self._page.evaluate("() => location.origin")
        result = await self._page.evaluate(
            """async ({url, headers}) => {
                if (location.origin !== 'https://arkm.com') throw new Error('Unexpected page origin');
                const response = await fetch(url, {
                    credentials: 'include', headers, redirect: 'error', signal: AbortSignal.timeout(30000)
                });
                return {
                    status: response.status, body: await response.text(),
                    retry_after: response.headers.get('Retry-After'),
                    cf_mitigated: response.headers.get('cf-mitigated')
                };
            }""",
            {"url": url, "headers": headers},
        )
        return Response(**result)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self) -> None:
        try:
            await self._context.close()
        finally:
            await self._driver.stop()


def persona_args(account: AccountConfig) -> list[str]:
    if account.persona != "windows":
        return []
    args = [
        f"--fingerprint={account.fingerprint_seed}",
        "--fingerprint-platform=windows",
        "--fingerprint-platform-version=15.0.0",
        "--fingerprint-brand=Chrome",
        "--fingerprint-hardware-concurrency=8",
    ]
    if account.disable_spoofing:
        args.append(f"--disable-spoofing={','.join(account.disable_spoofing)}")
    return args


class PlaywrightLauncher:
    async def launch(
        self, binary: Path, profile: Path, account: AccountConfig, proxy_port: int
    ) -> BrowserSession:
        from playwright.async_api import async_playwright

        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        driver = await async_playwright().start()
        try:
            context = await driver.chromium.launch_persistent_context(
                str(profile), executable_path=str(binary), headless=False,
                ignore_default_args=["--enable-automation"],
                no_viewport=True, chromium_sandbox=True,
                args=[
                    *persona_args(account),
                    f"--timezone={account.timezone}",
                    "--lang=en-US", "--accept-lang=en-US,en",
                    f"--proxy-server=socks5://127.0.0.1:{proxy_port}",
                    "--proxy-bypass-list=<-loopback>", "--disable-quic",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--window-position=0,0", "--window-size=1366,768",
                ],
            )
            page = context.pages[0] if context.pages else await context.new_page()
            return _PlaywrightSession(driver, context, page)
        except BaseException:
            with suppress(Exception):
                await driver.stop()
            raise


class _ProxyProcess:
    def __init__(self, process: asyncio.subprocess.Process, port: int):
        self._process = process
        self.port = port

    @property
    def alive(self) -> bool:
        return self._process.returncode is None

    async def wait(self) -> None:
        await self._process.wait()

    async def close(self) -> None:
        if self.alive:
            with suppress(ProcessLookupError):
                self._process.terminate()
        try:
            await asyncio.wait_for(self.wait(), 3)
        except TimeoutError:
            with suppress(ProcessLookupError):
                self._process.kill()
            await self.wait()
        self._process.stdin.close()


class PProxyForwarder:
    async def start(self, account: AccountConfig) -> Forward:
        payload = json.dumps(proxy_parts(account.proxy_url)).encode() + b"\n"
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tokenwatch.browser._proxy_worker",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(p or os.getcwd() for p in sys.path)},
        )
        forward = _ProxyProcess(process, 0)
        try:
            process.stdin.write(payload)
            await process.stdin.drain()
            ready = await asyncio.wait_for(process.stdout.readline(), 5)
            port = int(ready)
            if not 1 <= port <= 65535 or not forward.alive:
                raise ValueError
        except BaseException as error:
            with suppress(Exception):
                await forward.close()
            if not isinstance(error, Exception):
                raise
        else:
            forward.port = port
            return forward
        raise RuntimeError("Local proxy failed to start")
