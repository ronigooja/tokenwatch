import asyncio
import logging

import pytest
from sqlalchemy import select

from tokenwatch.config import Settings
from tokenwatch.db import Account, Event, Job, open_store
from tokenwatch.scheduler import RequestGate, Scheduler
from tokenwatch.upstream import Blocked


@pytest.mark.parametrize("outcome,account_id", [("success", "a"), ("blocked", "a"),
                                                  ("timeout", "a"), ("timeout", None)])
def test_shutdown_drains_without_penalty(tmp_path, caplog, outcome, account_id):
    async def run():
        settings = Settings(database=tmp_path / "db", bearer_token="x", signing_key="x",
                            request_interval=1, request_jitter=0)
        store = open_store(settings.database)
        with store.begin() as db:
            db.scalar(select(Job).where(Job.kind == "snapshots")).enabled = False
            db.add(Account(id="a", proxy_ref="p", failures=2))
            db.add(Job(id=2, kind="daily", account_id=account_id, interval_seconds=604800,
                       next_run=0, enabled=True, last_error="previous"))
        started, release = asyncio.Event(), asyncio.Event()

        class Fake:
            calls = 0

            async def get_json(self, *args):
                self.calls += 1
                started.set()
                await release.wait()
                if outcome == "blocked":
                    raise Blocked("forwarder stopped")
                return {"ok": True}

        fake = Fake()
        gate = RequestGate(store, fake, settings)

        async def handler(job):
            await gate.get(job.account_id, "/user", None)

        scheduler = Scheduler(store, settings, {"daily": handler}, gate=gate)
        scheduler.tick()
        await started.wait()
        drain = asyncio.create_task(scheduler.drain(gate, 0.01 if outcome == "timeout" else 1))
        await asyncio.sleep(0)
        scheduler.tick()
        with pytest.raises(asyncio.CancelledError):
            await gate.get("a", "/user", None)
        if outcome != "timeout":
            release.set()
        await drain
        with store() as db:
            account, job = db.get(Account, "a"), db.get(Job, 2)
            assert account.status == "active"
            assert account.failures == (0 if outcome == "success" else 2)
            assert account.available_at >= account.last_used_at + 1
            assert db.scalars(select(Event)).all() == []
            if outcome != "success":
                assert job.next_run == 0
                assert job.last_error == "previous"
        assert fake.calls == 1
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        store.kw["bind"].dispose()

    asyncio.run(run())


def test_real_blocked_still_pauses(tmp_path):
    async def run():
        settings = Settings(database=tmp_path / "db", bearer_token="x", signing_key="x")
        store = open_store(settings.database)
        with store.begin() as db:
            db.add(Account(id="a", proxy_ref="p"))

        class Fake:
            async def get_json(self, *args):
                raise Blocked()

        gate = RequestGate(store, Fake(), settings)
        with pytest.raises(Blocked):
            await gate.get("a", "/user", None)
        with store() as db:
            assert db.get(Account, "a").status == "paused"
            assert db.get(Account, "a").failures == 1
            assert db.scalar(select(Event.kind)) == "account_paused"
        store.kw["bind"].dispose()

    asyncio.run(run())
