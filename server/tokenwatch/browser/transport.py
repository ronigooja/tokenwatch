import asyncio
import json
import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import AccountConfig, proxy_parts

SITE_URL = "https://arkm.com/"
API_ORIGIN = "https://api.arkm.com"


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: str
    retry_after: str | None = None
    cf_mitigated: str | None = None


class BrowserSession(Protocol):
    async def open_site(self) -> None: ...
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
    def __init__(self, driver, context, page):
        self._driver = driver
        self._context = context
        self._page = page
        self._closed = asyncio.Event()
        context.on("close", lambda *_: self._closed.set())
        page.on("close", lambda *_: self._closed.set())

    async def open_site(self) -> None:
        await self._page.goto(SITE_URL, wait_until="domcontentloaded")

    async def title(self) -> str:
        return await self._page.title()

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
                viewport={"width": 1366, "height": 768},
                args=[
                    f"--fingerprint={account.fingerprint_seed}",
                    "--fingerprint-platform=windows",
                    "--fingerprint-platform-version=15.0.0",
                    "--fingerprint-brand=Chrome",
                    "--fingerprint-hardware-concurrency=8",
                    f"--timezone={account.timezone}",
                    "--lang=en-US", "--accept-lang=en-US,en", "--no-sandbox",
                    f"--proxy-server=socks5://127.0.0.1:{proxy_port}",
                    "--proxy-bypass-list=<-loopback>", "--disable-quic",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
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
