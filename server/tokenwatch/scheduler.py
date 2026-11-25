import asyncio
import json
import logging
import random
import time
from collections import defaultdict
from contextvars import ContextVar
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, delete, or_, select

from .db import Account, Job, Snapshot, Transfer, now
from .browser.client import _BrowserFailure
from .coins import CatalogRefreshFailure
from .rules import alert
from .upstream import AuthRequired, BadResponse, Blocked, RateLimited, RequestRejected, UNSUPPORTED_TOKEN, UpstreamError


logger = logging.getLogger(__name__)
_job_context = ContextVar("job_context", default=None)


def pause_details(exc, path, probe):
    job = _job_context.get()
    source = "status_probe" if probe else "job" if job else "other"
    result = {"source": source, "endpoint": safe_endpoint(path)}
    if isinstance(exc, Blocked):
        result["cause"] = exc.cause
    if source == "job":
        result.update(job_id=job[0], job_kind=job[1])
    for key in ("title", "page", "status", "screenshot", "click_attempts"):
        value = getattr(exc, key, None)
        if value is not None:
            result[key] = value
    if isinstance(exc, RequestRejected) and getattr(exc, "reject_detail", None) is not None:
        result["reject_detail"] = exc.reject_detail
    return result


def log_pause(account_id, details, label="account_paused"):
    logger.warning("%s account_id=%s cause=%s source=%s endpoint=%s title=%s path=%s job_id=%s job_kind=%s click_attempts=%s reject_detail=%s",
                   label, account_id, details.get("cause"), details["source"], details["endpoint"],
                   details.get("title", ""), details.get("page", ""), details.get("job_id"), details.get("job_kind"), details.get("click_attempts"),
                   json.dumps(details.get("reject_detail"), ensure_ascii=True))


class AccountUnavailable(Exception):
    pass


class DistributionWait(AccountUnavailable):
    def __init__(self, reason, retry_at):
        self.reason, self.retry_at = reason, retry_at


PAUSING_ERRORS = (AuthRequired, Blocked, RateLimited, RequestRejected)


class RequestGate:
    def __init__(self, store, upstream, settings, clock=time.time, sleep=asyncio.sleep, uniform=random.uniform):
        self.store = store
        self.upstream = upstream
        self.settings = settings
        self.clock = clock
        self.sleep = sleep
        self.uniform = uniform
        self.locks = defaultdict(asyncio.Lock)
        self.status_cache = {}
        self.status_locks = defaultdict(asyncio.Lock)
        self.closing = False
        self.in_flight = set()
        self.notify_account = lambda account_id: None

    def remember_status(self, account_id, status):
        old = self.status_cache.get(account_id, (None, None))[1]
        self.status_cache[account_id] = (self.clock(), status)
        if old != status:
            self.notify_account(account_id)

    def distribution_wait(self):
        with self.store() as db:
            active = db.scalars(select(Account).where(Account.status == "active").order_by(
                Account.available_at, Account.last_used_at, Account.id)).all()
        available = [a for a in active if self.status_cache.get(a.id, (None, None))[1] != "logged_out"]
        stamp = self.clock()
        if not available:
            return DistributionWait("no_eligible_account", stamp + 1)
        idle = [a for a in available if not self.locks[a.id].locked()]
        if not idle or (idle[0].available_at > stamp and any(self.locks[a.id].locked() for a in available)):
            return DistributionWait("waiting_for_in_flight_account_request", min(stamp + 1, idle[0].available_at) if idle else stamp + 1)
        if idle[0].available_at > stamp:
            return DistributionWait("waiting_for_account_request_gate", idle[0].available_at)
        return None

    async def account_status(self, account_id):
        async with self.status_locks[account_id]:
            cached = self.status_cache.get(account_id)
            if cached and self.clock() - cached[0] < self.settings.account_status_ttl:
                return cached[1]
            try:
                payload = await self.get(account_id, "/user", None, probe=True)
            except AuthRequired:
                return "logged_out"
            except Exception:
                return "unknown"
            if isinstance(payload, dict) and payload:
                return "logged_in"
            self.status_cache.pop(account_id, None)
            self.notify_account(account_id)
            return "unknown"

    async def get(self, account_id, path, params, decode=lambda value: value, probe=False, single=False):
        if self.closing:
            raise asyncio.CancelledError()
        task = asyncio.current_task()
        self.in_flight.add(task)
        try:
            return await self._dispatch(account_id, path, params, decode, probe, single)
        finally:
            self.in_flight.discard(task)

    async def _dispatch(self, account_id, path, params, decode, probe, single=False):
        if account_id is None:
            while True:
                if self.closing:
                    raise asyncio.CancelledError()
                wait = self.distribution_wait()
                if wait:
                    raise wait
                with self.store() as db:
                    active = db.scalars(select(Account).where(Account.status == "active").order_by(
                        Account.available_at, Account.last_used_at, Account.id)).all()
                available = [account for account in active if self.status_cache.get(account.id, (None, None))[1]
                             != "logged_out"]
                stamp = self.clock()
                if not available:
                    raise DistributionWait("no_eligible_account", stamp + 1)
                idle = [account for account in available if not self.locks[account.id].locked()]
                if not idle or (idle[0].available_at > stamp and any(
                    self.locks[account.id].locked() for account in available
                )):
                    retry_at = min(stamp + 1, idle[0].available_at) if idle else stamp + 1
                    raise DistributionWait("waiting_for_in_flight_account_request", retry_at)
                account = idle[0]
                if account.available_at > stamp:
                    raise DistributionWait("waiting_for_account_request_gate", account.available_at)
                try:
                    return await self._get(account.id, path, params, decode, probe)
                except PAUSING_ERRORS as exc:
                    if single:
                        raise
                    log_pause(account.id, pause_details(exc, path, probe), "account_retry")
                    continue
                except AccountUnavailable:
                    continue
        return await self._get(account_id, path, params, decode, probe)

    async def _get(self, account_id, path, params, decode, probe):
        async with self.locks[account_id]:
            while True:
                if self.closing:
                    raise asyncio.CancelledError()
                with self.store.begin() as db:
                    account = db.get(Account, account_id)
                    if not account or account.status != "active":
                        raise AccountUnavailable(account_id)
                    delay = account.available_at - self.clock()
                    if delay <= 0:
                        account.last_used_at = self.clock()
                        account.available_at = account.last_used_at + self.settings.request_interval + self.uniform(
                            0, self.settings.request_jitter
                        )
                if delay <= 0:
                    break
                await self.sleep(delay)
            try:
                payload = await self.upstream.get_json(account_id, path, params)
                result = decode(payload)
            except UpstreamError as exc:
                if self.closing or getattr(self.upstream, "_closed", False):
                    raise asyncio.CancelledError() from exc
                exc.endpoint = safe_endpoint(path)
                if probe and not isinstance(exc, PAUSING_ERRORS):
                    raise
                with self.store.begin() as db:
                    if account := db.get(Account, account_id):
                        account.failures += 1
                        backoff = min(self.settings.backoff_max,
                                      self.settings.backoff_initial * 2 ** min(account.failures - 1, 32))
                        if isinstance(exc, RateLimited) and exc.retry_after is not None:
                            backoff = max(backoff, exc.retry_after)
                        account.available_at = max(account.available_at, self.clock() + backoff)
                        if isinstance(exc, PAUSING_ERRORS):
                            account.status = "paused"
                            account.pause_reason = type(exc).__name__
                            details = pause_details(exc, path, probe)
                            log_pause(account_id, details)
                            alert(db, f"upstream:{uuid4()}", "account_paused",
                                  {"account_id": account_id, "reason": account.pause_reason, **details})
                if isinstance(exc, AuthRequired):
                    self.remember_status(account_id, "logged_out")
                self.notify_account(account_id)
                raise
            if payload is not UNSUPPORTED_TOKEN:
                with self.store.begin() as db:
                    if account := db.get(Account, account_id):
                        account.failures = 0
            self.remember_status(account_id, "logged_in")
            self.notify_account(account_id)
            return result


def safe_endpoint(path):
    if path.startswith("/token/addresses/"):
        return "/token/addresses/"
    return path if path in {"/intelligence/search", "/intelligence/entity_balance_changes", "/transfers", "/user"} else "unknown"


class Scheduler:
    def __init__(self, store, settings, handlers, clock=time.time, gate=None):
        self.store = store
        self.settings = settings
        self.handlers = handlers
        self.clock = clock
        self.gate = gate
        self.running = {}
        self.closing = False

    async def run_job(self, job: Job):
        token = _job_context.set((job.id, job.kind))
        try:
            return await self._run_job(job)
        finally:
            _job_context.reset(token)

    async def _run_job(self, job: Job):
        if self.closing:
            raise asyncio.CancelledError()
        next_run = self.clock() + job.interval_seconds
        wait_reason = None
        last_error = None
        success = False
        try:
            scheduled = await self.handlers[job.kind](job)
            success = True
            if scheduled is not None:
                next_run = scheduled
        except DistributionWait as exc:
            next_run, wait_reason = exc.retry_at, exc.reason
        except CatalogRefreshFailure as exc:
            next_run, last_error = exc.retry_at, exc.error[:200]
        except AccountUnavailable:
            next_run = self.clock()
        except UpstreamError as exc:
            last_error = self.log_failure(job, exc)
            with self.store() as db:
                if job.account_id is not None and (account := db.get(Account, job.account_id)):
                    next_run = max(self.clock(), account.available_at)
                elif job.account_id is None:
                    gate = self.gate or RequestGate(self.store, None, self.settings, clock=self.clock)
                    wait = gate.distribution_wait()
                    next_run = wait.retry_at if wait else self.clock()
                    wait_reason = wait.reason if wait else None
        except Exception as exc:
            last_error = self.log_failure(job, exc)
        with self.store.begin() as db:
            current = db.get(Job, job.id)
            if current and all(getattr(current, key) == getattr(job, key) for key in (
                "next_run", "enabled", "account_id", "kind", "coin_id", "options", "interval_seconds"
            )):
                current.next_run, current.wait_reason = next_run, wait_reason
                if success or last_error is not None:
                    current.last_error = last_error

    @staticmethod
    def log_failure(job, exc):
        endpoint = safe_endpoint(getattr(exc, "endpoint", ""))
        status = exc.status if isinstance(exc, BadResponse) and type(exc.status) is int else None
        category = type(exc).__name__ + (f":{status}" if status is not None else "")
        if isinstance(exc, _BrowserFailure):
            logger.warning("job_id=%s job_kind=%s endpoint=%s exception=%s status=%s detail=%s",
                           job.id, job.kind, endpoint, type(exc).__name__, status, exc.detail)
            return f"{category}[{exc.detail}] on {endpoint}"[:200]
        if isinstance(exc, BadResponse) and endpoint == "/token/addresses/" and hasattr(exc, "token_addresses_shape"):
            shape = exc.token_addresses_shape
            logger.warning("job_id=%s job_kind=%s endpoint=%s exception=%s status=%s shape=%s",
                           job.id, job.kind, endpoint, type(exc).__name__, status, shape)
            return f"{category}[{shape}] on {endpoint}"[:200]
        logger.warning("job_id=%s job_kind=%s endpoint=%s exception=%s status=%s",
                       job.id, job.kind, endpoint, type(exc).__name__, status)
        return f"{category} on {endpoint}"[:200]

    def tick(self):
        if self.closing:
            return
        self.running = {key: task for key, task in self.running.items() if not task.done()}
        with self.store() as db:
            jobs = db.scalars(select(Job).outerjoin(Account).where(
                Job.enabled, Job.next_run <= self.clock(), or_(Job.account_id.is_(None), and_(
                    Account.status == "active", Account.available_at <= self.clock())),
            ).order_by(Job.next_run, Job.id)).all()
        for job in jobs:
            key = job.account_id if job.account_id is not None else job.id
            if key not in self.running:
                self.running[key] = asyncio.create_task(self.run_job(job))

    def prune(self):
        with self.store.begin() as db:
            db.execute(delete(Transfer).where(Transfer.time < now() - timedelta(days=self.settings.transfer_retention_days)))
            db.execute(delete(Snapshot).where(Snapshot.created_at < now() - timedelta(days=self.settings.snapshot_retention_days)))

    async def run(self):
        cleanup = 0.0
        try:
            while True:
                if self.clock() >= cleanup:
                    self.prune()
                    cleanup = self.clock() + 3600
                self.tick()
                await asyncio.sleep(1)
        finally:
            if not self.closing:
                for task in self.running.values():
                    task.cancel()
                await asyncio.gather(*self.running.values(), return_exceptions=True)

    async def drain(self, gate, timeout):
        self.closing = True
        if gate:
            gate.closing = True
        tasks = set(self.running.values()) | (set(gate.in_flight) if gate else set())
        tasks.discard(asyncio.current_task())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
