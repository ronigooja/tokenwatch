import asyncio
from collections.abc import Callable
import hashlib
import json
import math
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar
from urllib.parse import urlencode

from ..upstream import AuthRequired, BadResponse, Blocked, RateLimited
from .config import AccountConfig, BrowserConfig, write_accounts
from .transport import (
    API_ORIGIN, BrowserSession, Forward, Forwarder, Launcher, PlaywrightLauncher,
    PProxyForwarder, Response,
)

T = TypeVar("T")


class _BrowserFailure(BadResponse):
    pass


def sign_payload(path: str, timestamp: int, key: str) -> str:
    inner = hashlib.sha256(f"{path}:{timestamp}:{key}".encode()).hexdigest()
    return hashlib.sha256(f"{key}:{inner}".encode()).hexdigest()


def _retry_after(value: str | None, now: float) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - now
        except (ValueError, TypeError, OverflowError, OSError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


def _invalid_json_constant(value: str) -> None:
    raise ValueError


def _decode(response: Response, now: float) -> Any:
    if response.status == 401 or "denied at anonymous" in response.body.lower():
        raise AuthRequired("Upstream login required")
    if response.status == 429:
        raise RateLimited("Upstream rate limited", retry_after=_retry_after(response.retry_after, now))
    if response.status == 403:
        body = response.body.lower()
        markers = ("just a moment", "/cdn-cgi/challenge-platform/", "cloudflare ray id")
        if (response.cf_mitigated or "").lower() == "challenge" or any(m in body for m in markers):
            raise Blocked("Upstream Cloudflare challenge")
    if not 200 <= response.status < 300:
        raise BadResponse(f"Unexpected upstream HTTP status: {response.status}")
    try:
        return json.loads(response.body, parse_constant=_invalid_json_constant)
    except (ValueError, RecursionError):
        pass
    raise BadResponse("Upstream response is not JSON")


async def _through(forward: Forward, action: Callable[[], Awaitable[T]]) -> T:
    if not forward.alive:
        raise Blocked("Local proxy stopped")

    async def run() -> T:
        return await action()

    operation = asyncio.create_task(run())
    exited = asyncio.create_task(forward.wait())
    try:
        done, _ = await asyncio.wait((operation, exited), return_when=asyncio.FIRST_COMPLETED)
        if exited in done or not forward.alive:
            raise Blocked("Local proxy stopped")
        if not operation.cancelled() and operation.exception() is not None:
            raise _BrowserFailure("Browser operation failed")
        return operation.result()
    finally:
        for task in (operation, exited):
            task.cancel()
        await asyncio.gather(operation, exited, return_exceptions=True)


@dataclass(slots=True)
class _Instance:
    session: BrowserSession
    forward: Forward
    idle: asyncio.TimerHandle | None = None
    watcher: asyncio.Task | None = None


class BrowserUpstreamClient:
    def __init__(
        self, config: BrowserConfig, *, launcher: Launcher | None = None,
        forwarder: Forwarder | None = None, clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._launcher = launcher if launcher is not None else PlaywrightLauncher()
        self._forwarder = forwarder if forwarder is not None else PProxyForwarder()
        self._clock = clock
        self._accounts = {account.id: account for account in config.accounts}
        self._managed_ids = set(config.managed_ids)
        self._locks = {account_id: asyncio.Lock() for account_id in self._accounts}
        self._changed = asyncio.Condition()
        self._instances: dict[str, _Instance] = {}
        self._starting = 0
        self._interactive: set[str] = set()
        self._failed_proxies: set[str] = set()
        self._closed = False

    async def __aenter__(self) -> "BrowserUpstreamClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def get_json(
        self, account_id: str, path: str, params: dict[str, str] | None = None,
    ) -> Any:
        if not re.fullmatch(r"/(?:[a-zA-Z0-9_~-][a-zA-Z0-9_.~/-]*)?", path):
            raise BadResponse("Invalid upstream URL path")
        if any(part in (".", "..") for part in path.split("/")):
            raise BadResponse("Invalid upstream URL path")
        url = API_ORIGIN + path + (f"?{urlencode(params)}" if params else "")
        async with self._lease(account_id) as instance:
            now = self._clock()
            timestamp = int(now)
            headers = {
                "X-Timestamp": str(timestamp),
                "X-Payload": sign_payload(path, timestamp, self._config.signing_key),
            }
            response = await _through(instance.forward, lambda: instance.session.fetch(url, headers))
            return _decode(response, self._clock())

    async def account_status(self, account_id: str) -> str:
        try:
            payload = await self.get_json(account_id, "/user")
            return "logged_in" if isinstance(payload, dict) and payload else "unknown"
        except AuthRequired:
            return "logged_out"
        except Exception:
            return "unknown"

    async def manual_login(self, account_id: str, *, ready: Callable[[], None] | None = None) -> None:
        async with self._lease(account_id, interactive=True) as instance:
            if ready:
                ready()
            await _through(instance.forward, instance.session.wait_closed)

    def account_config(self, account_id: str):
        account = self._accounts.get(account_id)
        return (account, account_id in self._managed_ids) if account else (None, False)

    async def add_account(self, account: AccountConfig) -> None:
        async with self._changed:
            if self._closed or self._config.accounts_file is None:
                raise BadResponse("Browser pool unavailable")
            if any(key.casefold() == account.id.casefold() for key in self._accounts):
                raise ValueError("Duplicate browser account ID")
            write_accounts(self._config.accounts_file, [self._accounts[key] for key in sorted(self._managed_ids)] + [account])
            self._accounts[account.id] = account
            self._managed_ids.add(account.id)
            self._locks[account.id] = asyncio.Lock()
            self._changed.notify_all()

    async def remove_account(self, account_id: str) -> None:
        lock = self._locks[account_id]
        async with lock, self._changed:
            if account_id not in self._managed_ids or self._config.accounts_file is None:
                raise ValueError("Account is not managed")
            write_accounts(self._config.accounts_file, [self._accounts[key] for key in sorted(self._managed_ids - {account_id})])
            if instance := self._instances.get(account_id):
                await self._drop(account_id, instance)
            del self._accounts[account_id]
            del self._locks[account_id]
            self._managed_ids.remove(account_id)
            self._failed_proxies.discard(account_id)
            self._changed.notify_all()

    async def _start(self, account: AccountConfig, interactive: bool) -> _Instance:
        try:
            forward = await self._forwarder.start(account)
        except Exception:
            forward = None
        if forward is None:
            raise Blocked("Local proxy failed to start")

        session = None
        ready = False

        async def launch() -> None:
            nonlocal session
            session = await self._launcher.launch(
                self._config.binary_path, self._config.profiles_root / account.id,
                account, forward.port,
            )

        try:
            await _through(forward, launch)
            assert session is not None
            await _through(forward, session.open_site)
            if not interactive:
                await self._wait_for_challenge(session, forward)
            ready = True
            return _Instance(session, forward)
        finally:
            if not ready:
                if not forward.alive:
                    self._failed_proxies.add(account.id)
                await self._shutdown(session, forward)

    async def _wait_for_challenge(self, session: BrowserSession, forward: Forward) -> None:
        async def challenged() -> bool:
            return "just a moment" in (await session.title()).lower()

        if not await _through(forward, challenged):
            return

        async def wait_until_clear() -> None:
            while await challenged():
                await asyncio.sleep(0.1)

        if self._config.cf_wait_seconds:
            try:
                async with asyncio.timeout(self._config.cf_wait_seconds):
                    await _through(forward, wait_until_clear)
                return
            except TimeoutError:
                pass
        raise Blocked("Browser Cloudflare challenge did not clear")

    @staticmethod
    async def _shutdown(session: BrowserSession | None, forward: Forward) -> None:
        try:
            if session is not None:
                with suppress(Exception):
                    await session.close()
        finally:
            with suppress(Exception):
                await forward.close()

    async def _drop(self, account_id: str, instance: _Instance) -> None:
        del self._instances[account_id]
        if instance.idle is not None:
            instance.idle.cancel()
        if instance.watcher is not None and instance.watcher is not asyncio.current_task():
            instance.watcher.cancel()
        if not instance.forward.alive:
            self._failed_proxies.add(account_id)
        try:
            await self._shutdown(instance.session, instance.forward)
        finally:
            self._changed.notify_all()

    async def _watch(self, account_id: str, instance: _Instance) -> None:
        await instance.forward.wait()
        async with self._changed:
            if self._instances.get(account_id) is instance:
                self._failed_proxies.add(account_id)
                if instance.idle is not None:
                    await self._drop(account_id, instance)

    async def _expire(self, account_id: str, instance: _Instance, timer: asyncio.TimerHandle) -> None:
        async with self._changed:
            if self._instances.get(account_id) is instance and instance.idle is timer:
                await self._drop(account_id, instance)

    @asynccontextmanager
    async def _lease(self, account_id: str, *, interactive: bool = False):
        account = self._accounts.get(account_id)
        if account is None:
            raise BadResponse("Unknown browser account")
        async with self._locks[account_id]:
            async with self._changed:
                if self._accounts.get(account_id) is not account:
                    raise BadResponse("Unknown browser account")
                while True:
                    if self._closed:
                        raise BadResponse("Browser pool is closed")
                    if account_id in self._failed_proxies:
                        raise Blocked("Local proxy stopped")
                    instance = self._instances.get(account_id)
                    if instance is not None:
                        if not instance.forward.alive:
                            await self._drop(account_id, instance)
                            raise Blocked("Local proxy stopped")
                        if instance.idle:
                            instance.idle.cancel()
                        instance.idle = None
                        if interactive:
                            self._interactive.add(account_id)
                        break
                    if interactive or len(self._instances.keys() - self._interactive) + self._starting < self._config.max_instances:
                        if interactive:
                            self._interactive.add(account_id)
                        else:
                            self._starting += 1
                        break
                    idle = [(slot.idle.when(), key, slot) for key, slot in self._instances.items() if slot.idle]
                    if idle:
                        _, key, slot = min(idle)
                        await self._drop(key, slot)
                    else:
                        await self._changed.wait()
            if instance is None:
                try:
                    instance = await self._start(account, interactive)
                finally:
                    async with self._changed:
                        if interactive:
                            if instance is None:
                                self._interactive.discard(account_id)
                        else:
                            self._starting -= 1
                        if instance is not None:
                            self._instances[account_id] = instance
                            instance.watcher = asyncio.create_task(self._watch(account_id, instance))
                        self._changed.notify_all()
            discard = False
            try:
                if self._closed:
                    raise BadResponse("Browser pool is closed")
                yield instance
            except (_BrowserFailure, asyncio.CancelledError):
                discard = True
                raise
            finally:
                async with self._changed:
                    self._interactive.discard(account_id)
                    if discard or self._closed or not instance.forward.alive:
                        await self._drop(account_id, instance)
                    else:
                        loop = asyncio.get_running_loop()

                        def expire() -> None:
                            loop.create_task(self._expire(account_id, instance, timer))

                        timer = loop.call_later(self._config.idle_timeout, expire)
                        instance.idle = timer
                        self._changed.notify_all()

    async def aclose(self) -> None:
        async with self._changed:
            self._closed = True
            self._changed.notify_all()
        for account_id, lock in self._locks.items():
            async with lock, self._changed:
                instance = self._instances.get(account_id)
                if instance is not None:
                    await self._drop(account_id, instance)
