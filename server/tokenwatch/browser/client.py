import asyncio
import hashlib
import json
import math
import logging
import os
import re
import secrets
import time
import unicodedata
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar
from urllib.parse import urlencode

from ..upstream import (AuthRequired, BadResponse, Blocked, RateLimited, RequestRejected,
                        TOKEN_ADDRESSES_PREFIX, UNSUPPORTED_TOKEN)
from .config import AccountConfig, BrowserConfig, write_accounts
from .manager import Manager, ManagerError, ManagerPage
from .transport import (
    API_ORIGIN, LOGIN_URL, REGISTER_URL, SITE_URL, BrowserSession, Forward, Forwarder,
    Launcher, PlaywrightLauncher, PProxyForwarder, Response,
)

T = TypeVar("T")
logger = logging.getLogger(__name__)


class _BrowserFailure(BadResponse):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__("Browser operation failed")


def _sanitize_detail(value: str) -> str:
    value = re.sub(r"https?://\S+", "<url>", value, flags=re.IGNORECASE)
    value = re.sub(r"\S+:\S+@\S+", "<redacted>", value)
    value = re.sub(r"\?\S*", "", value)
    value = re.sub(r"(?i)\b(cookie|token|authorization|password|proxy)\s*[:=]\s*\S+", r"\1=<redacted>", value)
    return " ".join(value.split())


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


def _reject_detail(body: str) -> str:
    def clean(value, limit):
        return " ".join("".join(char for char in value if not unicodedata.category(char).startswith("C")).split())[:limit]

    try:
        parsed = json.loads(body, parse_constant=_invalid_json_constant)
    except (ValueError, RecursionError):
        parsed, kind = None, "non-json"
    else:
        kind = "json-nonobject"
    if isinstance(parsed, dict):
        result = {"type": "object", "keys": [clean(key, 24) for key in list(parsed)[:8]]}
        if isinstance(parsed.get("message"), str):
            result["message"] = clean(parsed["message"], 120)
    else:
        length = len(body)
        result = {"type": kind, "length": "<=256" if length <= 256 else "<=2048" if length <= 2048 else ">2048"}
    return json.dumps(result, ensure_ascii=True, separators=(",", ":"))


def _decode(response: Response, now: float, path: str = "") -> Any:
    if response.status == 401 or "denied at anonymous" in response.body.lower():
        raise AuthRequired("Upstream login required")
    if response.status == 429:
        raise RateLimited("Upstream rate limited", retry_after=_retry_after(response.retry_after, now))
    if (response.cf_mitigated or "").lower() == "challenge":
        raise Blocked("Upstream Cloudflare challenge", cause="challenge_response", status=response.status)
    if response.status == 403:
        body = response.body.lower()
        markers = ("just a moment", "/cdn-cgi/challenge-platform/", "cloudflare ray id")
        if any(m in body for m in markers):
            raise Blocked("Upstream Cloudflare challenge", cause="challenge_403", status=response.status)
    if response.status == 400 and re.fullmatch(re.escape(TOKEN_ADDRESSES_PREFIX) + r"[a-zA-Z0-9_.-]+", path):
        try:
            body = json.loads(response.body, parse_constant=_invalid_json_constant)
        except (ValueError, RecursionError):
            body = None
        if isinstance(body, dict) and set(body) == {"message"}:
            return UNSUPPORTED_TOKEN
    if response.status in (400, 403):
        exc = RequestRejected(f"Unexpected upstream HTTP status: {response.status}", status=response.status)
        exc.reject_detail = _reject_detail(response.body)
        raise exc
    if not 200 <= response.status < 300:
        raise BadResponse(f"Unexpected upstream HTTP status: {response.status}", status=response.status)
    try:
        return json.loads(response.body, parse_constant=_invalid_json_constant)
    except (ValueError, RecursionError):
        pass
    raise BadResponse("Upstream response is not JSON", status=response.status)


async def _through(forward: Forward, action: Callable[[], Awaitable[T]], diagnostic=None) -> T:
    if not forward.alive:
        raise Blocked("Local proxy stopped", cause="forwarder_stopped")

    async def run() -> T:
        return await action()

    operation = asyncio.create_task(run())
    exited = asyncio.create_task(forward.wait())
    try:
        done, _ = await asyncio.wait((operation, exited), return_when=asyncio.FIRST_COMPLETED)
        if exited in done or not forward.alive:
            raise Blocked("Local proxy stopped", cause="forwarder_stopped")
        if not operation.cancelled() and (error := operation.exception()) is not None:
            try:
                message = str(error).splitlines()[0] if str(error) else ""
            except Exception:
                message = ""
            detail = _sanitize_detail(f"{type(error).__name__}: {message}").rstrip(": ")
            context = ""
            if diagnostic is not None:
                try:
                    context = await diagnostic()
                except Exception:
                    pass
            if context:
                detail = f"{detail[:100]} {context}"[:160]
            raise _BrowserFailure(detail[:160]) from None
        return operation.result()
    finally:
        for task in (operation, exited):
            task.cancel()
        await asyncio.gather(operation, exited, return_exceptions=True)


@dataclass(slots=True)
class _Instance:
    session: BrowserSession | ManagerPage
    forward: Forward | None = None
    idle: asyncio.TimerHandle | None = None
    watcher: asyncio.Task | None = None


class BrowserUpstreamClient:
    def __init__(
        self, config: BrowserConfig, *, launcher: Launcher | None = None,
        forwarder: Forwarder | None = None, clock: Callable[[], float] = time.time, autofill_timeout: float = 60,
        manager: Manager | None = None,
    ) -> None:
        self._config = config
        self._launcher = launcher if launcher is not None else PlaywrightLauncher()
        self._forwarder = forwarder if forwarder is not None else PProxyForwarder()
        self._manager = (manager if manager is not None else Manager(config.manager_socket)) if config.manager_socket else None
        self._login_pages: dict[str, ManagerPage] = {}
        self._login_autofill: dict[str, tuple[asyncio.Event, asyncio.Task]] = {}
        self._clock = clock
        self._autofill_timeout = autofill_timeout
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

    async def _blocked_capture(self, account_id, session, exc):
        if session is None:
            return
        try:
            context = getattr(session, "failure_context", None)
            if context:
                async with asyncio.timeout(1):
                    detail = await context()
                # failure_context only exposes approved static page paths and challenge titles.
                match = re.fullmatch(r"page=(\S*) title=(.*)", detail)
                if match:
                    exc.page = match.group(1)[:160]
                    exc.title = _sanitize_detail(match.group(2))[:60]
        except Exception:
            pass
        directory = self._config.blocked_screenshot_dir
        screenshot = getattr(session, "screenshot", None)
        if directory is None or screenshot is None:
            return
        name = f"{int(self._clock())}-{hashlib.sha256(account_id.encode()).hexdigest()[:8]}.png"
        path = directory / name
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            try:
                async with asyncio.timeout(5):
                    await screenshot(str(path))
                os.chmod(path, 0o600)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            exc.screenshot = name
            files = sorted((p for p in directory.iterdir() if re.fullmatch(r"\d+-[0-9a-f]{8}\.png", p.name)),
                           key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
            for old in files[self._config.blocked_screenshot_keep:]:
                old.unlink(missing_ok=True)
        except Exception:
            logger.debug("blocked screenshot capture failed")

    async def get_json(
        self, account_id: str, path: str, params: dict[str, str] | None = None,
    ) -> Any:
        if not re.fullmatch(r"/(?:[a-zA-Z0-9_~-][a-zA-Z0-9_.~/-]*)?", path):
            raise BadResponse("Invalid upstream URL path")
        if any(part in (".", "..") for part in path.split("/")):
            raise BadResponse("Invalid upstream URL path")
        url = API_ORIGIN + path + (f"?{urlencode(params)}" if params else "")
        timeout = asyncio.timeout(None)
        try:
            async with timeout:
                async with self._lease(account_id, deadline=timeout) as instance:
                    if instance.forward is None:
                        try:
                            await instance.session.check_site()
                        except ManagerError as error:
                            raise _BrowserFailure(f"Browser Manager: {error.code}") from None
                    timestamp = int(self._clock())
                    headers = {
                        "X-Timestamp": str(timestamp),
                        "X-Payload": sign_payload(path, timestamp, self._config.signing_key),
                    }
                    if instance.forward is None:
                        try:
                            response = await instance.session.fetch(url, headers)
                        except ManagerError as error:
                            raise _BrowserFailure(f"Browser Manager: {error.code}") from None
                    else:
                        response = await _through(instance.forward, lambda: instance.session.fetch(url, headers),
                                                   getattr(instance.session, "failure_context", None))
                    return _decode(response, self._clock(), path)
        except TimeoutError:
            if not timeout.expired():
                raise
            raise _BrowserFailure("TimeoutError: browser request timed out") from None

    async def account_status(self, account_id: str) -> str:
        try:
            payload = await self.get_json(account_id, "/user")
            return "logged_in" if isinstance(payload, dict) and payload else "unknown"
        except AuthRequired:
            return "logged_out"
        except Exception:
            return "unknown"

    async def manual_login(self, account_id: str, *, ready: Callable[[], None] | None = None, page: str = "login") -> None:
        if self._manager is not None:
            await self._manager_login(account_id, ready, page)
            return
        async with self._lease(account_id, interactive=True) as instance:
            if ready:
                ready()
            account = self._accounts[account_id]
            await _through(instance.forward, lambda: instance.session.login_page(
                page, account.email, account.password, self._autofill_timeout))
            await _through(instance.forward, instance.session.wait_closed)

    @property
    def manager_mode(self) -> bool:
        return self._manager is not None

    async def _manager_login(self, account_id, ready, page):
        async with self._locks[account_id]:
            if self._closed:
                raise BadResponse("Browser pool is closed")
            session = ManagerPage(self._manager, self._config.manager_profiles[account_id],
                                  f"tokenwatch-login-{secrets.token_hex(16)}")
            autofill = None
            try:
                async with asyncio.timeout(self._config.request_timeout):
                    url = {"login": LOGIN_URL, "register": REGISTER_URL, "maintenance": SITE_URL}[page]
                    await session.open(url, persistent=False)
                    generation = (await session.info())["generation"]
                self._login_pages[account_id] = session
                if ready:
                    ready()
                account = self._accounts[account_id]
                if page == "login" and account.email and account.password and self._autofill_timeout > 0:
                    stop = asyncio.Event()
                    autofill = asyncio.create_task(self._manager_autofill(session, account.email, account.password, stop))
                    self._login_autofill[account_id] = (stop, autofill)
                while True:
                    await asyncio.sleep(1)
                    async with asyncio.timeout(self._config.close_timeout):
                        if (await session.info())["generation"] != generation:
                            return
            finally:
                if autofill is not None:
                    autofill.cancel()
                    await asyncio.gather(autofill, return_exceptions=True)
                    self._login_autofill.pop(account_id, None)
                self._login_pages.pop(account_id, None)
                await self._close_manager_page(session)

    async def _manager_autofill(self, session, email, password, stop):
        try:
            async with asyncio.timeout(self._autofill_timeout):
                while not stop.is_set():
                    if await session.fill_credentials(LOGIN_URL.partition("?")[0], email, password):
                        return
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=1)
                    except TimeoutError:
                        pass
        except (Exception, asyncio.CancelledError):
            # Autofill is best-effort; errors can contain credentials and must not be logged.
            pass

    async def login_view(self, account_id, input_data=None):
        session = self._login_pages.get(account_id)
        if session is None:
            raise ManagerError("not_found")
        async with asyncio.timeout(self._config.close_timeout):
            if input_data is None:
                return await session.png()
            await session.call("input", **input_data)

    async def _close_manager_page(self, session):
        try:
            async with asyncio.timeout(self._config.close_timeout):
                await session.close()
        except (ManagerError, TimeoutError):
            logger.warning("Browser Manager page close failed")

    def account_config(self, account_id: str):
        account = self._accounts.get(account_id)
        return (account, account_id in self._managed_ids) if account else (None, False)

    async def add_account(self, account: AccountConfig) -> None:
        if self.manager_mode:
            raise ValueError("Browser Manager accounts require operator provisioning")
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
        if self.manager_mode:
            raise ValueError("Browser Manager accounts require operator removal")
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

    async def replace_account(self, account: AccountConfig) -> None:
        async with self._locks[account.id], self._changed:
            if self._closed or account.id not in self._managed_ids or self._config.accounts_file is None:
                raise BadResponse("Browser pool unavailable")
            if self.manager_mode and account.proxy_url != self._accounts[account.id].proxy_url:
                raise ValueError("Browser Manager routes require operator changes")
            write_accounts(self._config.accounts_file, [account if key == account.id else self._accounts[key]
                                                        for key in sorted(self._managed_ids)])
            if instance := self._instances.get(account.id):
                await self._drop(account.id, instance)
            self._accounts[account.id] = account
            self._failed_proxies.discard(account.id)
            self._changed.notify_all()

    async def _start(self, account: AccountConfig, interactive: bool) -> _Instance:
        if self._manager is not None:
            page = ManagerPage(self._manager, self._config.manager_profiles[account.id], "tokenwatch-automation")
            try:
                await page.open(SITE_URL)
            except ManagerError as error:
                await self._close_manager_page(page)
                raise _BrowserFailure(f"Browser Manager: {error.code}") from None
            except asyncio.CancelledError:
                await self._close_manager_page(page)
                raise
            return _Instance(page)
        try:
            forward = await self._forwarder.start(account)
        except Exception:
            forward = None
        if forward is None:
            raise Blocked("Local proxy failed to start", cause="forwarder_start_failed")

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
                configure = getattr(session, "configure_page_settle", None)
                if configure is not None:
                    configure(self._config.page_settle_seconds)
                settle = getattr(session, "settle", None)
                if settle is not None and self._config.page_settle_seconds:
                    await _through(forward, lambda: settle(self._config.page_settle_seconds))
            ready = True
            return _Instance(session, forward)
        except Blocked as exc:
            await self._blocked_capture(account.id, session, exc)
            raise
        finally:
            if not ready:
                if not forward.alive:
                    self._failed_proxies.add(account.id)
                await self._shutdown(account.id, session, forward)

    @staticmethod
    def _challenge_box(info):
        boxes = info.get("boxes", [])
        if len(boxes) != 1:
            return None, "missing_or_ambiguous_box"
        box = boxes[0]
        try:
            x, y, width, height = (box[k] for k in ("x", "y", "width", "height"))
            viewport = info["viewport"]
            valid = (all(type(v) in (int, float) and math.isfinite(v) for v in (x, y, width, height))
                     and width >= 200 and height >= 40 and x >= 0 and y >= 0
                     and x + width <= viewport["width"] and y + height <= viewport["height"])
        except (KeyError, TypeError):
            valid = False
        return (box, None) if valid else (None, "implausible_box")

    async def _challenge_episode(self, session, enabled, state):
        loop = asyncio.get_running_loop()
        end = loop.time() + self._config.cf_wait_seconds
        next_click = loop.time() + self._config.cf_click_initial_wait_seconds
        attempts = 0
        reason = "disabled" if not self._config.cf_click_enabled else "unsupported"
        inspect = enabled
        can_click = enabled
        while True:
            info = None
            if inspect:
                try:
                    info = await session.challenge_info()
                except Exception:
                    inspect = can_click = False
                    reason = "inspection_failed"
            active = info["challenge"] if info is not None else "just a moment" in (await session.title()).lower()
            if not active:
                return attempts, "cleared" if attempts else "skipped", "challenge_cleared" if attempts else "cleared_without_click", True
            now = loop.time()
            if now >= end:
                break
            if can_click and info.get("failure"):
                can_click, reason = False, "failure_state"
            if can_click and now >= next_click:
                if attempts >= self._config.cf_click_max_attempts:
                    can_click, reason = False, "attempt_limit"
                elif end - now < self._config.cf_click_wait_seconds:
                    can_click, reason = False, "insufficient_time"
                else:
                    box, error = self._challenge_box(info)
                    if error:
                        can_click, reason = False, error
                    else:
                        attempts += 1
                        state.update(attempts=attempts, result="failed", reason="attempt_limit")
                        try:
                            await session.click_challenge(box, attempts)
                        except Exception:
                            can_click, reason = False, "click_error"
                        else:
                            next_click = loop.time() + self._config.cf_click_wait_seconds
                            reason = "attempt_limit"
            state["reason"] = reason
            delay = 1 if attempts and inspect else 0.1
            if can_click:
                delay = min(delay, max(0.001, next_click - loop.time()))
            await asyncio.sleep(min(delay, max(0, end - loop.time())))
        return attempts, "failed" if attempts else "skipped", reason, False

    async def _wait_for_challenge(self, session: BrowserSession, forward: Forward) -> None:
        async def challenged():
            return "just a moment" in (await session.title()).lower()

        if not await _through(forward, challenged):
            return
        enabled = (self._config.cf_click_enabled and callable(getattr(session, "challenge_info", None))
                   and callable(getattr(session, "click_challenge", None)))
        state = {"attempts": 0, "result": "skipped",
                 "reason": "disabled" if not self._config.cf_click_enabled else "unsupported"}
        # The timeout also bounds slow inspection and mouse operations.
        async def episode():
            attempts, result, reason, cleared = await self._challenge_episode(session, enabled, state)
            state.update(attempts=attempts, result=result, reason=reason)
            return cleared
        try:
            if self._config.cf_wait_seconds:
                async with asyncio.timeout(self._config.cf_wait_seconds):
                    if await _through(forward, episode):
                        return
        except TimeoutError:
            pass
        finally:
            logger.info("cf_click attempts=%s result=%s reason=%s", state["attempts"], state["result"], state["reason"])
        exc = Blocked("Browser Cloudflare challenge did not clear", cause="challenge_timeout")
        exc.click_attempts = state["attempts"]
        raise exc

    async def _shutdown(self, account_id: str, session: BrowserSession | None, forward: Forward) -> None:
        from .transport import force_kill_browser

        tasks = [asyncio.create_task(forward.close())]
        if session is not None:
            tasks.append(asyncio.create_task(session.close()))
        try:
            done, pending = await asyncio.wait(tasks, timeout=self._config.close_timeout)
            failed = bool(pending) or any(task.cancelled() or task.exception() for task in done)
        except asyncio.CancelledError:
            failed = True
            raise
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
                else:
                    with suppress(BaseException):
                        task.result()
            if locals().get("failed", True):
                await force_kill_browser(self._config.profiles_root / account_id, session)

    async def _drop(self, account_id: str, instance: _Instance) -> None:
        del self._instances[account_id]
        if instance.idle is not None:
            instance.idle.cancel()
        if instance.watcher is not None and instance.watcher is not asyncio.current_task():
            instance.watcher.cancel()
        if instance.forward is not None and not instance.forward.alive:
            self._failed_proxies.add(account_id)
        try:
            if instance.forward is None:
                await self._close_manager_page(instance.session)
            else:
                await self._shutdown(account_id, instance.session, instance.forward)
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
    async def _lease(self, account_id: str, *, interactive: bool = False, deadline=None):
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
                        raise Blocked("Local proxy stopped", cause="forwarder_stopped")
                    instance = self._instances.get(account_id)
                    if instance is not None:
                        if deadline is not None:
                            deadline.reschedule(asyncio.get_running_loop().time() + self._config.request_timeout)
                        if instance.forward is not None and not instance.forward.alive:
                            exc = Blocked("Local proxy stopped", cause="forwarder_stopped")
                            await self._blocked_capture(account_id, instance.session, exc)
                            await self._drop(account_id, instance)
                            raise exc
                        if instance.idle:
                            instance.idle.cancel()
                        instance.idle = None
                        if interactive:
                            self._interactive.add(account_id)
                        break
                    if interactive or len(self._instances.keys() - self._interactive) + self._starting < self._config.max_instances:
                        if deadline is not None:
                            deadline.reschedule(asyncio.get_running_loop().time() + self._config.request_timeout)
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
                            if instance.forward is not None:
                                instance.watcher = asyncio.create_task(self._watch(account_id, instance))
                        self._changed.notify_all()
            discard = False
            try:
                if self._closed:
                    raise BadResponse("Browser pool is closed")
                yield instance
            except Blocked as exc:
                try:
                    await self._blocked_capture(account_id, instance.session, exc)
                except asyncio.CancelledError:
                    discard = True
                    raise
                raise
            except (_BrowserFailure, asyncio.CancelledError):
                discard = True
                raise
            finally:
                async with self._changed:
                    self._interactive.discard(account_id)
                    if discard or self._closed or (instance.forward is not None and not instance.forward.alive):
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
        if self._manager is not None:
            await self._manager.close()
