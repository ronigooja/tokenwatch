import asyncio
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from test_core import balance, core, settings  # noqa: F401
from tokenwatch.api import create_app
from tokenwatch.collectors import BALANCE_PATH, Collectors
from tokenwatch.db import Account, Coin, Exclusion, Job, Snapshot, SnapshotCapture, SnapshotRow, SnapshotSchedule, open_store
from tokenwatch.scheduler import RequestGate, Scheduler
from tokenwatch.upstream import RateLimited


def test_automatic_snapshot_rotation_waits_failure_and_restart(core, settings):
    store, fake, clock, gate, collectors = core
    with store.begin() as db:
        db.add_all([
            Coin(id="duplicate", symbol="ZZZ", pricing_id="abc", status="supported"),
            Coin(id="case", symbol="ABC", pricing_id="AbC", status="supported"),
            Coin(id="native:ZZ", symbol="NATIVE", pricing_id="native", status="supported"),
            Coin(id="excluded", symbol="AAA", pricing_id="excluded", status="supported"),
            Coin(id="pending", symbol="PENDING", pricing_id="pending", status="pending_support"),
            Coin(id="unmapped", symbol="UNMAPPED", status="supported"),
        ])
        db.flush()
        db.add(Exclusion(coin_id="excluded", reason="test"))
        for account in db.scalars(select(Account)):
            account.status = "paused"
        job = db.scalar(select(Job).where(Job.kind == "snapshots"))
        job_id = job.id

    async def tick(scheduler):
        scheduler.tick()
        await asyncio.gather(*scheduler.running.values())

    async def run():
        scheduler = Scheduler(store, settings, {"snapshots": collectors.snapshots}, clock, gate)
        await tick(scheduler)
        assert not fake.calls
        with store.begin() as db:
            job = db.get(Job, job_id)
            assert not job.enabled and job.interval_seconds == 604800
            job.enabled, job.interval_seconds = True, 300
        await tick(scheduler)
        with store() as db:
            assert db.get(Job, job_id).wait_reason == "no_eligible_account"
            assert db.get(SnapshotSchedule, 1).next_slot_at == 0
        clock.value += 500
        with store.begin() as db:
            for account in db.scalars(select(Account)):
                account.status = "active"
                account.available_at = 1600
        await tick(scheduler)
        with store() as db:
            assert db.get(Job, job_id).wait_reason == "waiting_for_account_request_gate"
            assert db.get(SnapshotSchedule, 1).next_slot_at == 0
            assert not db.scalars(select(SnapshotCapture)).all()
        assert not fake.calls
        fake.responses.extend([RateLimited(retry_after=1000), [balance(str(i)) for i in range(50)], [], []])
        clock.value = 1800
        await tick(scheduler)
        await tick(scheduler)
        with store() as db:
            assert db.get(Job, job_id).next_run == 1900
            assert db.get(Job, job_id).last_error == f"RateLimited on {BALANCE_PATH}"
            assert db.get(Account, "a").available_at == 2800
            assert db.get(Account, "a").status == "paused"
            assert db.get(SnapshotCapture, "AbC").last_captured_at is None
        assert [(account, params["pricingIds"]) for account, _, params in fake.calls] == [("a", "AbC")]
        clock.value = 1900
        await tick(scheduler)
        with store() as db:
            snapshot = db.scalar(select(Snapshot))
            assert snapshot.truncated and snapshot.automatic_pricing_id == "abc"
            assert len(db.scalars(select(SnapshotRow)).all()) == 50
        with store.begin() as db:
            # Job reconfiguration or an interrupted scheduler can leave this due after restart.
            db.get(Job, job_id).next_run = 0
        reopened = open_store(settings.database)
        try:
            gate2 = RequestGate(reopened, fake, settings, clock=clock, sleep=clock.sleep)
            collector2 = Collectors(reopened, gate2, settings, clock=clock)
            resumed = Scheduler(reopened, settings, {"snapshots": collector2.snapshots}, clock, gate2)
            await tick(resumed)
            assert len(fake.calls) == 2
            with reopened() as db:
                assert db.get(Job, job_id).next_run == 2000
            clock.value = 2500
            await tick(resumed)
            await tick(resumed)
            assert len(fake.calls) == 3
            clock.value = 2600
            await tick(resumed)
        finally:
            reopened.kw["bind"].dispose()
        assert [params["pricingIds"] for _, _, params in fake.calls] == ["AbC", "abc", "native", "AbC"]
        assert all(path == BALANCE_PATH and params == {
            "interval": "7d", "orderBy": "balance_usd_pct_change", "orderDir": "desc", "limit": "50",
            "offset": "0", "pricingIds": params["pricingIds"],
        } for _, path, params in fake.calls)

    asyncio.run(run())


def test_in_flight_snapshot_keeps_requested_identity_and_does_not_catch_up(core, settings):
    store, fake, clock, gate, collectors = core
    with store.begin() as db:
        job = db.scalar(select(Job).where(Job.kind == "snapshots"))
        job.enabled, job.interval_seconds = True, 100
        job_id = job.id

    get_json = fake.get_json

    async def slow_fetch(*args):
        clock.value += 300
        with store.begin() as db:
            db.get(Coin, "ABC").pricing_id = "different"
            db.add(Exclusion(coin_id="ABC", reason="operator change"))
        return await get_json(*args)

    fake.get_json = slow_fetch
    fake.responses.append([])

    async def run():
        scheduler = Scheduler(store, settings, {"snapshots": collectors.snapshots}, clock, gate)
        scheduler.tick()
        await asyncio.gather(*scheduler.running.values())
        scheduler.tick()
        assert not scheduler.running

    asyncio.run(run())
    with store() as db:
        assert db.get(Job, job_id).next_run == 1400
        assert db.scalar(select(Snapshot)).automatic_pricing_id == "abc"
        assert db.get(SnapshotCapture, "abc").last_captured_at == datetime.utcfromtimestamp(1300)
        assert not db.get(SnapshotCapture, "different")


def test_restart_after_unsettled_reservation_waits_a_fresh_slot(core, settings):
    store, fake, clock, gate, collectors = core
    with store.begin() as db:
        job = db.scalar(select(Job).where(Job.kind == "snapshots"))
        job.enabled, job.interval_seconds = True, 100
        job_id = job.id
        schedule = db.get(SnapshotSchedule, 1)
        schedule.next_slot_at, schedule.pending_spacing = 800, 100
        db.add(SnapshotCapture(pricing_id="abc", last_attempt_at=700))
    fake.responses.append([])

    async def run():
        scheduler = Scheduler(store, settings, {"snapshots": collectors.snapshots}, clock, gate)
        scheduler.tick()
        await asyncio.gather(*scheduler.running.values())
        assert not fake.calls
        with store() as db:
            assert db.get(Job, job_id).next_run == 1100
            assert db.get(SnapshotSchedule, 1).pending_spacing is None
        clock.value = 1100
        scheduler.tick()
        await asyncio.gather(*scheduler.running.values())
        assert len(fake.calls) == 1

    asyncio.run(run())


def test_snapshot_api_filters_and_jobs(settings):
    app = create_app(settings, run_scheduler=False)
    headers = {"Authorization": "Bearer test-token"}
    captured_at = datetime(2026, 1, 1)
    # Treat the API identifier as opaque, including characters outside the CoinRef allowlist.
    exact_id = "AbC+/ Ω"
    with TestClient(app) as client:
        assert client.get("/snapshot-coins").status_code == 401
        client.headers.update(headers)
        with app.state.store.begin() as db:
            db.add_all([
                Coin(id="one", symbol="ZZZ", pricing_id=exact_id, status="supported"),
                Coin(id="two", symbol="AAA", pricing_id=exact_id, status="supported"),
                Coin(id="three", symbol="BBB", pricing_id=exact_id.lower(), status="supported"),
                Coin(id="four", symbol="CCC", pricing_id="lost", status="excluded"),
                SnapshotCapture(pricing_id=exact_id, last_attempt_at=1000, last_captured_at=captured_at),
                Snapshot(id=1, interval="14d", pricing_ids=[exact_id], truncated=False),
                Snapshot(id=2, interval="7d", pricing_ids=[exact_id], automatic_pricing_id=exact_id, truncated=False),
                Snapshot(id=3, interval="7d", pricing_ids=["lost"], automatic_pricing_id="lost", truncated=False),
                Snapshot(id=4, interval="7d", pricing_ids=[exact_id], automatic_pricing_id=exact_id, truncated=False),
            ])
        assert client.get("/snapshot-coins").json() == [
            {"id": exact_id, "symbol": "AAA", "last_captured_at": "2026-01-01T00:00:00Z", "snapshot_count": 2},
            {"id": exact_id.lower(), "symbol": "BBB", "last_captured_at": None, "snapshot_count": 0},
        ]
        for params, ids in (({}, [4, 3, 2, 1]), ({"coin_id": exact_id}, [4, 2]),
                            ({"coin_id": exact_id, "limit": 1, "offset": 1}, [2]),
                            ({"coin_id": exact_id.lower()}, []), ({"coin_id": "lost"}, [3]), ({"legacy": True}, [1])):
            assert [item["id"] for item in client.get("/snapshots", params=params).json()] == ids
        assert client.get("/snapshots", params={"coin_id": exact_id, "legacy": True}).status_code == 422
        assert client.get("/snapshots/2").json()["pricing_ids"] == [exact_id]
        assert client.get("/snapshots/1").json()["interval"] == "14d"
        assert client.post("/snapshots", json={}).status_code == 405
        job, = client.get("/jobs").json()
        assert job["kind"] == "snapshots" and not job["enabled"]
        url = f"/jobs/{job['id']}"
        update = {"kind": "snapshots", "interval_seconds": 600, "enabled": True, "options": {}}
        assert client.put(url, json={**update, "options": {"balance_min": 10}}).status_code == 422
        assert client.put(url, json={**update, "account_id": "a"}).status_code == 422
        assert client.put(url, json={**update, "coin_id": "one"}).status_code == 422
        assert client.put(url, json=update).json()["interval_seconds"] == 600
        assert client.post("/jobs", json=update).status_code == 409


def test_v8_migration_preserves_manual_snapshots_and_seeds_once(settings):
    store = open_store(settings.database)
    with store.begin() as db:
        db.add(Snapshot(id=7, interval="30d", pricing_ids=["legacy"], truncated=False, balance_min=1.5))
        db.flush()
        db.add(SnapshotRow(snapshot_id=7, entity_id="entity"))
        db.flush()
        db.execute(text("DELETE FROM jobs WHERE kind = 'snapshots'"))
        db.execute(text("DROP INDEX ix_snapshots_automatic_pricing_id"))
        db.execute(text("ALTER TABLE snapshots DROP COLUMN automatic_pricing_id"))
        db.execute(text("DROP TABLE snapshot_captures"))
        db.execute(text("DROP TABLE snapshot_schedule"))
        db.execute(text("PRAGMA user_version=8"))
    store.kw["bind"].dispose()
    migrated = open_store(settings.database)
    with migrated.begin() as db:
        assert db.scalar(text("PRAGMA user_version")) == 9
        legacy = db.get(Snapshot, 7)
        assert legacy.automatic_pricing_id is None and legacy.balance_min == 1.5 and legacy.pricing_ids == ["legacy"]
        assert db.get(SnapshotRow, (7, "entity"))
        job = db.scalar(select(Job).where(Job.kind == "snapshots"))
        assert not job.enabled and job.interval_seconds == 604800
        db.delete(job)
    migrated.kw["bind"].dispose()
    reopened = open_store(settings.database)
    with reopened() as db:
        assert db.scalar(select(Job).where(Job.kind == "snapshots")) is None
    reopened.kw["bind"].dispose()
