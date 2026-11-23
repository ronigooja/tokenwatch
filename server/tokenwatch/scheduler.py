import asyncio
import logging
import random
import time
from collections import defaultdict
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, delete, or_, select

from .db import Account, Job, Snapshot, Transfer, now
from .rules import alert
from .upstream import AuthRequired, Blocked, RateLimited, RequestRejected, UNSUPPORTED_TOKEN, UpstreamError


logger = logging.getLogger(__name__)


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

    def remember_status(self, account_id, status):
        self.status_cache[account_id] = (self.clock(), status)

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
            return "unknown"

    async def get(self, account_id, path, params, decode=lambda value: value, probe=False):
        if account_id is None:
            while True:
                with self.store() as db:
                    active = db.scalars(select(Account).where(Account.status == "active").order_by(
                        Account.available_at, Account.last_used_at, Account.id)).all()
                available = [account for account in active if self.status_cache.get(account.id, (None, None))[1]
                             == "logged_in"]
                stamp = self.clock()
                if not available:
                    raise DistributionWait("no_active_account_with_confirmed_session", stamp + 1)
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
                except (AccountUnavailable, *PAUSING_ERRORS):
                    continue
        return await self._get(account_id, path, params, decode, probe)

    async def _get(self, account_id, path, params, decode, probe):
        async with self.locks[account_id]:
            while True:
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
                if isinstance(exc, AuthRequired):
                    self.remember_status(account_id, "logged_out")
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
                            alert(db, f"upstream:{uuid4()}", "account_paused",
                                  {"account_id": account_id, "reason": account.pause_reason})
                raise
            if payload is not UNSUPPORTED_TOKEN:
                with self.store.begin() as db:
                    if account := db.get(Account, account_id):
                        account.failures = 0
            self.remember_status(account_id, "logged_in")
            return result


class Scheduler:
    def __init__(self, store, settings, handlers, clock=time.time):
        self.store = store
        self.settings = settings
        self.handlers = handlers
        self.clock = clock
        self.running = {}

    async def run_job(self, job: Job):
        next_run = self.clock() + job.interval_seconds
        wait_reason = None
        try:
            scheduled = await self.handlers[job.kind](job)
            if scheduled is not None:
                next_run = scheduled
        except DistributionWait as exc:
            next_run, wait_reason = exc.retry_at, exc.reason
        except AccountUnavailable:
            next_run = self.clock()
        except UpstreamError:
            with self.store() as db:
                if job.account_id is not None and (account := db.get(Account, job.account_id)):
                    next_run = max(self.clock(), account.available_at)
        except Exception:
            logger.exception("Job %s failed", job.id)
        with self.store.begin() as db:
            current = db.get(Job, job.id)
            if current and all(getattr(current, key) == getattr(job, key) for key in (
                "next_run", "enabled", "account_id", "kind", "coin_id", "options", "interval_seconds"
            )):
                current.next_run, current.wait_reason = next_run, wait_reason

    def tick(self):
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
            for task in self.running.values():
                task.cancel()
            await asyncio.gather(*self.running.values(), return_exceptions=True)
