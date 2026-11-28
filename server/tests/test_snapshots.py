import asyncio
from datetime import datetime

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, text

from test_core import balance, core, settings  # noqa: F401
from tokenwatch.api import create_app
from tokenwatch.collectors import BALANCE_PATH, Collectors
from tokenwatch.db import Account, Coin, Exclusion, Job, Snapshot, SnapshotCapture, SnapshotRow, SnapshotSchedule, open_store
from tokenwatch.scheduler import RequestGate, Scheduler
from tokenwatch.upstream import RateLimited


@pytest.fixture
def enable_snapshot_job():
    return True


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
            assert job.enabled and job.interval_seconds == 604800
            job.interval_seconds = 300
            job.next_run = 0
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
    fake.responses.extend([[], []])

    async def run():
        scheduler = Scheduler(store, settings, {"snapshots": collectors.snapshots}, clock, gate)
        scheduler.tick()
        await asyncio.gather(*scheduler.running.values())
        scheduler.tick()
        assert not scheduler.running
        clock.value = 1400
        scheduler.tick()
        await asyncio.gather(*scheduler.running.values())
        assert len(fake.calls) == 1
        with store.begin() as db:
            assert db.get(Job, job_id).wait_reason == "no_eligible_snapshot_coin"
            db.delete(db.get(Exclusion, "ABC"))
            db.get(Coin, "ABC").symbol = "NEW"
        fake.get_json = get_json
        # The first newly eligible coin is picked up on the next tick, not in a week.
        clock.value = 1401
        scheduler.tick()
        await asyncio.gather(*scheduler.running.values())
        assert [params["pricingIds"] for _, _, params in fake.calls] == ["abc", "different"]

    asyncio.run(run())
    with store() as db:
        assert db.get(Job, job_id).next_run == 1501
        old = db.get(SnapshotCapture, "abc")
        assert old.last_captured_at == datetime.utcfromtimestamp(1300) and old.last_symbol == "ABC"
        snapshot_id = db.scalars(select(Snapshot.id).where(Snapshot.automatic_pricing_id == "abc")).one()
    app = create_app(settings, run_scheduler=False)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        snapshots = {row["id"]: row for row in client.get("/snapshot-coins").json()}
        assert snapshots["abc"]["symbol"] == "ABC" and snapshots["abc"]["snapshot_count"] == 1
        assert snapshots["different"]["symbol"] == "NEW"
        assert [row["id"] for row in client.get("/snapshots", params={"coin_id": "abc"}).json()] == [snapshot_id]


def test_generic_failure_after_reservation_keeps_next_slot(core, settings):
    store, fake, clock, gate, collectors = core
    with store.begin() as db:
        db.add(Coin(id="B", symbol="B", status="supported", pricing_id="b"))
        job = db.scalar(select(Job).where(Job.kind == "snapshots"))
        job.interval_seconds = 200
        job_id = job.id
    fake.responses.extend([RuntimeError("synthetic post-reservation failure"), []])

    async def run():
        scheduler = Scheduler(store, settings, {"snapshots": collectors.snapshots}, clock, gate)
        scheduler.tick()
        await asyncio.gather(*scheduler.running.values())
        with store() as db:
            job = db.get(Job, job_id)
            assert job.next_run == 1100 and job.last_error == "RuntimeError on unknown"
            assert job.next_run == db.get(SnapshotSchedule, 1).next_slot_at
        clock.value = 1100
        scheduler.tick()
        await asyncio.gather(*scheduler.running.values())
        assert [params["pricingIds"] for _, _, params in fake.calls] == ["abc", "b"]

    asyncio.run(run())


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
    exact_id = " AbC+/ Ω "
    with TestClient(app) as client:
        assert client.get("/snapshot-coins").status_code == 401
        client.headers.update(headers)
        with app.state.store.begin() as db:
            db.add_all([
                Coin(id="one", symbol="ZZZ", pricing_id=exact_id, status="supported"),
                Coin(id="two", symbol="AAA", pricing_id=exact_id, status="supported"),
                Coin(id="three", symbol="BBB", pricing_id=exact_id.lower(), status="supported"),
                Coin(id="four", symbol="CCC", pricing_id="lost", status="excluded"),
                Coin(id="five", symbol="DDD", pricing_id="expired", status="supported"),
                SnapshotCapture(pricing_id=exact_id, last_attempt_at=1000, last_captured_at=captured_at, last_symbol="AAA"),
                SnapshotCapture(pricing_id="lost", last_attempt_at=1000, last_captured_at=captured_at, last_symbol="OLD"),
                SnapshotCapture(pricing_id="expired", last_attempt_at=1000, last_captured_at=captured_at, last_symbol="OLD"),
                Snapshot(id=2, interval="7d", pricing_ids=[exact_id], automatic_pricing_id=exact_id, truncated=False),
                Snapshot(id=3, interval="7d", pricing_ids=["lost"], automatic_pricing_id="lost", truncated=False),
                Snapshot(id=4, interval="7d", pricing_ids=[exact_id], automatic_pricing_id=exact_id, truncated=False),
            ])
            db.flush()
            db.add_all([
                SnapshotRow(snapshot_id=2, entity_id="z"),
                SnapshotRow(snapshot_id=2, entity_id="a", balance_unit=7, prev_balance_unit=3, token_balances=[]),
            ])
        assert client.get("/snapshot-coins").json() == [
            {"id": exact_id, "symbol": "AAA", "last_captured_at": "2026-01-01T00:00:00Z", "snapshot_count": 2},
            {"id": exact_id.lower(), "symbol": "BBB", "last_captured_at": None, "snapshot_count": 0},
            {"id": "expired", "symbol": "DDD", "last_captured_at": "2026-01-01T00:00:00Z", "snapshot_count": 0},
            {"id": "lost", "symbol": "OLD", "last_captured_at": "2026-01-01T00:00:00Z", "snapshot_count": 1},
        ]
        for params, ids in (({"coin_id": exact_id}, [4, 2]),
                            ({"coin_id": exact_id, "limit": 1, "offset": 1}, [2]),
                            ({"coin_id": exact_id.lower()}, []), ({"coin_id": exact_id.strip()}, []),
                            ({"coin_id": "lost"}, [3])):
            assert [item["id"] for item in client.get("/snapshots", params=params).json()] == ids
        for params in ({}, {"legacy": True}):
            missing = client.get("/snapshots", params=params)
            assert missing.status_code == 422 and missing.json()["detail"][0]["loc"] == ["query", "coin_id"]
        paths = app.openapi()["paths"]
        assert {param["name"] for param in paths["/snapshots"]["get"]["parameters"]} == {"coin_id", "limit", "offset"}
        assert "/snapshots/compare" not in paths
        assert client.get("/snapshots/compare?before=2&after=4").status_code == 422
        detail = client.get("/snapshots/2").json()
        summary = client.get("/snapshots", params={"coin_id": exact_id, "offset": 1}).json()[0]
        assert set(summary) == {"id", "created_at", "interval", "truncated"}
        rows = detail.pop("rows")
        assert detail == summary and summary["interval"] == "7d"
        assert [row["entity_id"] for row in rows] == ["a", "z"]
        assert rows[0] == {"entity_id": "a", "entity_name": None, "entity_type": None, "balance_unit": 7,
                           "balance_usd": None, "prev_balance_unit": 3, "prev_balance_usd": None}
        assert client.get("/snapshots/1").status_code == 404
        assert client.post("/snapshots", json={}).status_code == 405
        job, = client.get("/jobs").json()
        assert job["kind"] == "snapshots" and job["enabled"]
        url = f"/jobs/{job['id']}"
        update = {"kind": "snapshots", "interval_seconds": 600, "enabled": True, "options": {}}
        assert client.put(url, json={**update, "options": {"balance_min": 10}}).status_code == 422
        assert client.put(url, json={**update, "account_id": "a"}).status_code == 422
        assert client.put(url, json={**update, "coin_id": "one"}).status_code == 422
        assert client.put(url, json=update).json()["interval_seconds"] == 600
        assert client.post("/jobs", json=update).status_code == 409


def test_v8_migration_removes_manual_snapshots_and_seeds_once(settings):
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
        assert db.scalar(text("PRAGMA user_version")) == 10
        assert db.get(Snapshot, 7) is None
        assert db.get(SnapshotRow, (7, "entity")) is None
        job = db.scalar(select(Job).where(Job.kind == "snapshots"))
        assert job.enabled and job.interval_seconds == 604800
        db.delete(job)
    migrated.kw["bind"].dispose()
    reopened = open_store(settings.database)
    with reopened() as db:
        assert db.scalar(select(Job).where(Job.kind == "snapshots")) is None
    reopened.kw["bind"].dispose()


def test_v9_migration_removes_only_manual_snapshots_and_their_rows(settings):
    store = open_store(settings.database)
    captured_at = datetime(2026, 1, 1)
    with store.begin() as db:
        db.add_all([
            Snapshot(id=1, interval="30d", pricing_ids=["AbC"], balance_min=1.5, truncated=False),
            Snapshot(id=2, interval="7d", pricing_ids=["AbC"], automatic_pricing_id="AbC", truncated=True,
                     created_at=captured_at),
            Snapshot(id=3, interval="14d", truncated=False),
            Snapshot(id=4, interval="7d", pricing_ids=["AbC"], automatic_pricing_id="AbC", truncated=False),
        ])
        db.flush()
        db.add_all([
            SnapshotRow(snapshot_id=1, entity_id="shared"),
            SnapshotRow(snapshot_id=1, entity_id="manual"),
            SnapshotRow(snapshot_id=2, entity_id="shared", balance_unit=10, token_balances={"AbC": 10}),
            SnapshotRow(snapshot_id=3, entity_id="shared"),
            SnapshotRow(snapshot_id=4, entity_id="shared", prev_balance_unit=10),
        ])
        db.add_all([
            SnapshotCapture(pricing_id="AbC", last_attempt_at=1000, last_captured_at=captured_at, last_symbol="ABC"),
            SnapshotCapture(pricing_id="failed", last_attempt_at=1100),
        ])
        schedule = db.get(SnapshotSchedule, 1)
        schedule.next_slot_at, schedule.pending_spacing = 1200, 100
        job = db.scalar(select(Job).where(Job.kind == "snapshots"))
        job.enabled, job.interval_seconds, job.next_run, job.wait_reason = False, 600, 1250, "test_wait"
        db.execute(text("PRAGMA user_version=9"))
    store.kw["bind"].dispose()

    for _ in range(2):
        migrated = open_store(settings.database)
        with migrated() as db:
            assert db.scalar(text("PRAGMA user_version")) == 10
            assert db.scalar(text("PRAGMA foreign_keys")) == 1
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
            first, second = db.scalars(select(Snapshot).order_by(Snapshot.id)).all()
            assert (first.id, first.automatic_pricing_id, first.pricing_ids, first.created_at, first.truncated) == (
                2, "AbC", ["AbC"], captured_at, True)
            assert (second.id, second.automatic_pricing_id, second.truncated) == (4, "AbC", False)
            rows = db.scalars(select(SnapshotRow).order_by(SnapshotRow.snapshot_id)).all()
            assert [(row.snapshot_id, row.entity_id, row.balance_unit, row.prev_balance_unit, row.token_balances)
                    for row in rows] == [(2, "shared", 10, None, {"AbC": 10}), (4, "shared", None, 10, None)]
            capture, failed = db.scalars(select(SnapshotCapture).order_by(SnapshotCapture.pricing_id)).all()
            assert (capture.pricing_id, capture.last_attempt_at, capture.last_captured_at, capture.last_symbol) == (
                "AbC", 1000, captured_at, "ABC")
            assert (failed.pricing_id, failed.last_attempt_at, failed.last_captured_at, failed.last_symbol) == (
                "failed", 1100, None, None)
            schedule = db.get(SnapshotSchedule, 1)
            assert (schedule.next_slot_at, schedule.pending_spacing) == (1200, 100)
            job = db.scalars(select(Job).where(Job.kind == "snapshots")).one()
            assert (job.enabled, job.interval_seconds, job.next_run, job.wait_reason) == (False, 600, 1250, "test_wait")
        migrated.kw["bind"].dispose()
