import asyncio
import logging
import random
import time
from collections import defaultdict
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, select

from .db import Account, Job, Snapshot, Transfer, now
from .rules import alert
from .upstream import AuthRequired, Blocked, RateLimited, UpstreamError


logger = logging.getLogger(__name__)


class AccountUnavailable(Exception):
    pass


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
                payload = await self.get(account_id, "/user", None)
                status = "logged_in" if isinstance(payload, dict) and payload else "unknown"
            except AuthRequired:
                status = "logged_out"
            except Exception:
                status = "unknown"
            self.remember_status(account_id, status)
            return status

    async def get(self, account_id, path, params, decode=lambda value: value):
        async with self.locks[account_id]:
            while True:
                with self.store.begin() as db:
                    account = db.get(Account, account_id)
                    if not account or account.status != "active":
                        raise AccountUnavailable(account_id)
                    delay = account.available_at - self.clock()
                    if delay <= 0:
                        account.available_at = self.clock() + self.settings.request_interval + self.uniform(
                            0, self.settings.request_jitter
                        )
                if delay <= 0:
                    break
                await self.sleep(delay)
            try:
                result = decode(await self.upstream.get_json(account_id, path, params))
            except UpstreamError as exc:
                if isinstance(exc, AuthRequired):
                    self.remember_status(account_id, "logged_out")
                with self.store.begin() as db:
                    if account := db.get(Account, account_id):
                        account.failures += 1
                        backoff = min(self.settings.backoff_max,
                                      self.settings.backoff_initial * 2 ** min(account.failures - 1, 32))
                        if isinstance(exc, RateLimited) and exc.retry_after is not None:
                            backoff = max(backoff, exc.retry_after)
                        account.available_at = max(account.available_at, self.clock() + backoff)
                        if isinstance(exc, (AuthRequired, Blocked, RateLimited)):
                            account.status = "paused"
                            account.pause_reason = type(exc).__name__
                            alert(db, f"upstream:{uuid4()}", "account_paused",
                                  {"account_id": account_id, "reason": account.pause_reason})
                raise
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
        try:
            scheduled = await self.handlers[job.kind](job)
            if scheduled is not None:
                next_run = scheduled
        except AccountUnavailable:
            next_run = self.clock()
        except UpstreamError:
            with self.store() as db:
                if account := db.get(Account, job.account_id):
                    next_run = max(self.clock(), account.available_at)
        except Exception:
            logger.exception("Job %s failed", job.id)
        with self.store.begin() as db:
            current = db.get(Job, job.id)
            if current and all(getattr(current, key) == getattr(job, key) for key in (
                "next_run", "enabled", "account_id", "kind", "coin_id", "options", "interval_seconds"
            )):
                current.next_run = next_run

    def tick(self):
        self.running = {key: task for key, task in self.running.items() if not task.done()}
        with self.store() as db:
            jobs = db.scalars(select(Job).join(Account).where(
                Job.enabled, Job.next_run <= self.clock(), Account.status == "active",
                Account.available_at <= self.clock(),
            ).order_by(Job.next_run, Job.id)).all()
        for job in jobs:
            if job.account_id not in self.running:
                self.running[job.account_id] = asyncio.create_task(self.run_job(job))

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
