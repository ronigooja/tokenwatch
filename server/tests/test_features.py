import asyncio
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from test_core import core, enable_snapshot_job, settings, transfer
from tokenwatch.api import create_app
from tokenwatch.coins import public_json
from tokenwatch.db import Account, Coin, DailyBalance, now
from tokenwatch.upstream import AuthRequired, BadResponse, Blocked, RateLimited


@pytest.mark.parametrize("span,expected", [(99, 49.5), (6000, 3000), (0, 3600)])
def test_first_transfer_window(core, span, expected):
    store, fake, clock, gate, collectors = core
    rows = [transfer(str(i)) for i in range(100)]
    origin = now()
    for i, row in enumerate(rows):
        row["blockTimestamp"] = (origin + timedelta(seconds=i * span / 99)).isoformat()
    fake.responses.append({"transfers": rows})
    result = asyncio.run(collectors.transfers(SimpleNamespace(coin_id="ABC", account_id="a", interval_seconds=1)))
    assert result - clock.value == pytest.approx(expected)


@pytest.mark.parametrize("response,status", [({"id": "user"}, "logged_in"),
                                            (AuthRequired(), "logged_out")])
def test_session_cache_and_gate(core, response, status):
    store, fake, clock, gate, collectors = core
    fake.responses.append(response)
    async def run():
        assert await gate.account_status("a") == status
        assert await gate.account_status("a") == status
    asyncio.run(run())
    assert len(fake.calls) == 1
    assert fake.calls[0][1] == "/user"


@pytest.mark.parametrize("response", [BadResponse(), [], {}])
def test_unknown_status_is_not_cached_and_does_not_penalize_the_account(core, response):
    store, fake, clock, gate, collectors = core
    fake.responses.extend([response, {"id": "user"}])
    async def run():
        assert await gate.account_status("a") == "unknown"
        assert await gate.account_status("a") == "logged_in"
    asyncio.run(run())
    assert len(fake.calls) == 2
    with store.begin() as db:
        account = db.get(Account, "a")
        assert (account.failures, account.status) == (0, "active")


@pytest.mark.parametrize("error", [Blocked(), RateLimited(retry_after=500)])
def test_status_probe_still_pauses_on_blocked_or_rate_limited(core, error):
    store, fake, clock, gate, collectors = core
    fake.responses.append(error)
    assert asyncio.run(gate.account_status("a")) == "unknown"
    with store.begin() as db:
        account = db.get(Account, "a")
        assert (account.failures, account.status) == (1, "paused")
        assert account.pause_reason == type(error).__name__


def test_scheduler_refreshes_session_cache(core):
    store, fake, clock, gate, collectors = core
    fake.responses.extend([{}, AuthRequired()])
    async def run():
        await gate.get("a", "/transfers", {})
        assert await gate.account_status("a") == "logged_in"
        with pytest.raises(AuthRequired):
            await gate.get("a", "/transfers", {})
        assert await gate.account_status("a") == "logged_out"
    asyncio.run(run())
    assert len(fake.calls) == 2


def test_session_ttl(core):
    store, fake, clock, gate, collectors = core
    fake.responses.extend([{"id": "u"}, BadResponse(), {"id": "u"}])
    async def run():
        assert await gate.account_status("a") == "logged_in"
        clock.value += gate.settings.account_status_ttl - 1
        assert await gate.account_status("a") == "logged_in"
        clock.value += 1
        assert await gate.account_status("a") == "unknown"
        assert await gate.account_status("a") == "logged_in"
    asyncio.run(run())
    assert len(fake.calls) == 3


def test_entity_selection_api(settings):
    app = create_app(settings, run_scheduler=False)
    with TestClient(app) as client:
        client.headers.update({"Authorization": "Bearer test-token"})
        with app.state.store.begin() as db:
            db.add(Coin(id="ABC", symbol="ABC"))
            db.flush()
            for ago, entity, amount in [(0, "a", 2), (1, "a", 1), (2, "b", 3), (40, "old", 4)]:
                db.add(DailyBalance(coin_id="ABC", day=now().date()-timedelta(days=ago), entity_id=entity,
                                   entity_name=entity.upper(), entity_type="exchange", balance_unit=amount,
                                   balance_usd=amount * 100))
        result = client.get("/coins/ABC/entities?days=3").json()
        assert [row["entity_id"] for row in result] == ["a", "b"]
        assert result[0]["balance_unit"] == 2
        assert len(client.get("/coins/ABC/entities?days=1").json()) == 1
        assert client.get("/coins/ABC/entities?days=0").status_code == 422
        assert client.get("/coins/missing/entities").status_code == 404


def test_public_request_bounded_backoff_and_validation():
    calls, delays = [], []
    def fetch(request):
        calls.append(request)
        return httpx.Response(429, json={})
    async def sleep(delay):
        delays.append(delay)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await public_json(client, "https://example.invalid/catalog", sleep)
    asyncio.run(run())
    assert len(calls) == 3
    assert delays == [30, 60]


def test_api_session_reads_share_cache(settings):
    from test_core import FakeUpstream
    fake = FakeUpstream()
    fake.responses.append({"id": "user"})
    app = create_app(settings, fake, catalog_transport=httpx.MockTransport(lambda request: httpx.Response(500)), run_scheduler=False)
    with TestClient(app) as client:
        client.headers.update({"Authorization": "Bearer test-token"})
        with app.state.store.begin() as db:
            db.add(Account(id="a", proxy_ref="p", status="active"))
        client.put("/accounts/a", json={"status": "active"})
        for _ in range(3):
            assert client.get("/accounts/a/session").json() == {"status": "logged_in"}
    assert len(fake.calls) == 1


def test_account_responses_use_last_cached_status_without_probes(settings):
    from test_core import Clock, FakeUpstream
    fake, clock = FakeUpstream(), Clock()
    fake.account_config = lambda account_id: (
        SimpleNamespace(timezone="UTC", persona="windows", email="managed@example.com",
                        password="secret", proxy_url="secret-proxy"), account_id == "managed"
    )
    fake.responses.extend([{"id": "user"}, AuthRequired(), BadResponse()])
    app = create_app(settings, fake, catalog_transport=httpx.MockTransport(lambda request: httpx.Response(500)), run_scheduler=False)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        app.state.gate.clock = clock
        app.state.gate.sleep = clock.sleep
        with app.state.store.begin() as db:
            db.add_all([Account(id="managed", proxy_ref="p"), Account(id="static", proxy_ref="q"),
                        Account(id="failed", proxy_ref="r")])
        rows = {row["id"]: row for row in client.get("/accounts").json()}
        assert all((row["session_status"], row["session_checked_at"]) == (None, None) for row in rows.values())
        assert not fake.calls
        assert client.get("/accounts/managed/session").json()["status"] == "logged_in"
        clock.value += 10
        assert client.get("/accounts/static/session").json()["status"] == "logged_out"
        clock.value += 10
        assert client.get("/accounts/failed/session").json()["status"] == "unknown"
        clock.value += settings.account_status_ttl + 1
        calls = list(fake.calls)
        rows = {row["id"]: row for row in client.get("/accounts").json()}
        assert (rows["managed"]["session_status"], rows["managed"]["session_checked_at"]) == ("logged_in", 1000)
        assert (rows["static"]["session_status"], rows["static"]["session_checked_at"]) == ("logged_out", 1010)
        assert (rows["failed"]["session_status"], rows["failed"]["session_checked_at"]) == (None, None)
        assert rows["managed"]["password"] == "secret"
        assert rows["static"]["password"] is None and rows["static"]["proxy_url"] is None
        assert client.get("/accounts/managed").json() == rows["managed"]
        assert client.get("/accounts/static").json() == rows["static"]
        assert fake.calls == calls
