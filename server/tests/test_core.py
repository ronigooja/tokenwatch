import asyncio
import json
from collections import deque
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import MockTransport, Response
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from tokenwatch.api import create_app
from tokenwatch.coins import ALPHA_URL, SPOT_URL, Candidate, CoinCatalog, KnownPricingIDResolver, binance_candidates
from tokenwatch.collectors import Collectors
from tokenwatch.config import Settings, load_settings
from tokenwatch.db import (Account, Coin, CoinMapping, DailyBalance, DailyCollection, Event, Exclusion,
                           Job, PushLog, Rule, Subscription, Transfer, TransferCursor, open_store)
from tokenwatch.scheduler import AccountUnavailable, RequestGate, Scheduler
from tokenwatch.upstream import AuthRequired, BadResponse, Blocked, RateLimited


class FakeUpstream:
    def __init__(self):
        self.responses = deque()
        self.calls = []

    async def get_json(self, account_id, path, params=None):
        self.calls.append((account_id, path, params))
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    async def account_status(self, account_id):
        return "logged_in"


class Clock:
    value = 1000

    def __call__(self):
        return self.value

    async def sleep(self, seconds):
        self.value += seconds


@pytest.fixture
def settings(tmp_path):
    return Settings(database=tmp_path / "test.db", bearer_token="test-token", signing_key="test-key",
                    request_interval=0.001, request_jitter=0, transfer_min_interval=10,
                    transfer_max_interval=3600, backoff_initial=20, backoff_max=100)


@pytest.fixture
def core(settings):
    store = open_store(settings.database)
    with store.begin() as db:
        db.add_all([Account(id="a", proxy_ref="p"), Account(id="b", proxy_ref="q"),
                    Coin(id="ABC", symbol="ABC", pricing_id="abc", verified=True)])
        db.flush()
        db.add(Subscription(coin_id="ABC"))
    fake, clock = FakeUpstream(), Clock()
    gate = RequestGate(store, fake, settings, clock=clock, sleep=clock.sleep)
    collectors = Collectors(store, gate, settings, clock=clock)
    yield store, fake, clock, gate, collectors
    store.kw["bind"].dispose()


def transfer(id, usd=500):
    return {"id": id, "transactionHash": f"tx-{id}", "blockTimestamp": "2026-01-10T00:00:00Z",
            "historicalUSD": usd, "fromAddress": {"address": "sender", "chain": "ethereum"},
            "toAddress": {"address": "recipient", "arkhamEntity": {"id": "e"}}}


def balance(id, amount=1):
    return {"entity": {"id": id, "name": id.upper(), "type": "exchange"}, "balanceUnit": amount,
            "balanceUsd": amount * 100, "prevBalanceUnit": 1, "prevBalanceUsd": 100}


def test_config_environment_and_migration(settings, tmp_path, monkeypatch):
    path = tmp_path / "config.local.toml"
    path.write_text(f'database = "{settings.database}"\nbearer_token = "file"\nsigning_key = "file"\n')
    monkeypatch.setenv("TOKENWATCH_CONFIG", str(path))
    monkeypatch.setenv("TOKENWATCH_SIGNING_KEY", "env")
    loaded = load_settings()
    assert loaded.signing_key.get_secret_value() == "env"
    assert loaded.bearer_token.get_secret_value() == "file"
    for _ in range(2):
        store = open_store(loaded.database)
        with store() as db:
            assert db.scalar(text("PRAGMA journal_mode")) == "wal"
            assert db.scalar(text("PRAGMA user_version")) == 1
        store.kw["bind"].dispose()


@pytest.mark.parametrize("error", [AuthRequired(), Blocked(), RateLimited(retry_after=90)])
def test_scheduler_pauses_and_respects_backoff(core, settings, error):
    store, fake, clock, gate, _ = core
    async def handler(job):
        await gate.get(job.account_id, "/path", {})
    with store.begin() as db:
        db.add(Job(account_id="a", kind="daily", interval_seconds=5, enabled=True))
    scheduler = Scheduler(store, settings, {"daily": handler}, clock=clock)
    fake.responses.extend([error, {"other": True}, {"ok": True}])

    async def run():
        scheduler.tick()
        await scheduler.running["a"]
        with store() as db:
            account = db.get(Account, "a")
            assert account.status == "paused"
            assert account.pause_reason == type(error).__name__
            assert account.available_at == 1000 + (90 if isinstance(error, RateLimited) else 20)
            assert db.scalar(select(Job.next_run)) == account.available_at
            assert db.scalar(select(Event.kind)) == "account_paused"
        with pytest.raises(AccountUnavailable):
            await gate.get("a", "/path", {})
        assert await gate.get("b", "/path", {}) == {"other": True}
        with store.begin() as db:
            db.get(Account, "a").status = "active"
        scheduler.tick()
        assert "a" not in scheduler.running
        clock.value += 90
        scheduler.tick()
        await scheduler.running["a"]
        with store() as db:
            assert db.get(Account, "a").failures == 0
        assert [call[0] for call in fake.calls] == ["a", "b", "a"]

    asyncio.run(run())


def test_exponential_backoff_and_request_jitter(core):
    store, fake, clock, gate, _ = core
    gate.settings = gate.settings.model_copy(update={"request_interval": 5, "request_jitter": 3})
    gate.uniform = lambda lower, upper: upper
    fake.responses.extend([BadResponse(), BadResponse(), [], []])

    async def run():
        for deadline in (1020, 1060):
            with pytest.raises(BadResponse):
                await gate.get("a", "/path", {})
            with store() as db:
                account = db.get(Account, "a")
                assert account.status == "active"
                assert account.available_at == deadline
        await gate.get("a", "/path", {})
        assert clock.value == 1060
        await gate.get("a", "/path", {})
        assert clock.value == 1068

    asyncio.run(run())


def test_transfers_dedup_adaptive_and_gap(core):
    store, fake, clock, _, collectors = core
    job = SimpleNamespace(coin_id="ABC", account_id="a", interval_seconds=120)
    fake.responses.extend([{"transfers": payload} for payload in (
        [transfer("a"), transfer("a")], [transfer("b"), transfer("a")], [transfer("c")],
    )])
    assert asyncio.run(collectors.transfers(job)) == 4600
    clock.value += 100
    assert asyncio.run(collectors.transfers(job)) == 4700
    clock.value += 100
    assert asyncio.run(collectors.transfers(job)) == 3000
    with store() as db:
        assert db.scalar(select(func.count()).select_from(Transfer)) == 3
        assert db.get(Transfer, "c").gap is True
        assert db.get(Transfer, "a").gap is False
        assert db.get(TransferCursor, "ABC").rate == pytest.approx(0.01)
        assert db.scalar(select(Event.kind)) == "transfer_gap"
    for _, path, params in fake.calls:
        assert path == "/transfers"
        assert int(params["offset"]) + int(params["limit"]) <= 100


def test_daily_truncation_exclusion_and_balance_rule(core):
    store, fake, _, _, collectors = core
    with store.begin() as db:
        db.add(Coin(id="NO", symbol="NO", pricing_id="no", verified=True))
        db.add(Rule(name="daily", enabled=True, source="balance_diff", level="normal", push=False,
                    conditions={"min_balance_change_pct": 50, "direction": "increase"}))
        db.flush()
        db.add(Exclusion(coin_id="NO", reason="test"))
    fake.responses.extend([[balance(str(i)) for i in range(50)], [balance("0", 2)]])
    day = date(2026, 1, 10)
    collectors.today = lambda: day
    job = SimpleNamespace(account_id="a", options={"balance_min": 200}, interval_seconds=3600)

    async def run():
        await collectors.daily(job)
        await collectors.daily(job)

    asyncio.run(run())
    day = date(2026, 1, 11)
    asyncio.run(collectors.daily(job))
    with store() as db:
        assert db.get(DailyCollection, (date(2026, 1, 10), "ABC")).truncated
        assert not db.get(DailyCollection, (day, "ABC")).truncated
        assert db.get(DailyBalance, (day, "ABC", "0")).balance_unit == 2
        assert db.scalar(select(func.count()).select_from(DailyBalance)) == 51
        events = {event.kind: event for event in db.scalars(select(Event))}
        assert events["balance_diff"].data["balance_change_pct"] == 100
        assert events["daily_truncated"].push
    assert len(fake.calls) == 2
    assert fake.calls[0][2] == {"interval": "7d", "orderBy": "balance_usd_pct_change", "orderDir": "desc",
                                "balanceMin": "200", "pricingIds": "abc", "limit": "50", "offset": "0"}


def test_transfer_rules_cooldown_merge_and_filters(core):
    store, fake, clock, _, collectors = core
    conditions = {"coins_include": ["ABC"], "min_usd": 300, "entities_include": ["e"], "direction": "inbound"}
    with store.begin() as db:
        db.add_all([
            Rule(name="cooldown", enabled=True, source="transfer", conditions=conditions,
                 level="normal", push=False, cooldown_seconds=600),
            Rule(name="merge", enabled=True, source="transfer", conditions=conditions,
                 level="important", push=True, merge_seconds=600),
            Rule(name="excluded", enabled=True, source="transfer", conditions={**conditions, "coins_exclude": ["ABC"]},
                 level="log", push=False),
        ])
    job = SimpleNamespace(coin_id="ABC", account_id="a", interval_seconds=10)
    fake.responses.extend([{"transfers": [transfer("a"), transfer("small", 100)]},
                           {"transfers": [transfer("b"), transfer("a"), transfer("small", 100)]}])
    asyncio.run(collectors.transfers(job))
    clock.value += 10
    asyncio.run(collectors.transfers(job))
    with store() as db:
        by_level = {event.level: event for event in db.scalars(select(Event))}
        assert set(by_level) == {"normal", "important"}
        assert by_level["normal"].count == 1
        assert by_level["important"].count == 2
        assert by_level["important"].data["id"] == "b"


def test_coin_catalog_requires_mapping_and_opt_in(core, settings):
    store, fake, _, gate, _ = core
    async def candidates():
        return [Candidate("SPOT", [{"feed": "spot"}]), Candidate("ALPHA", [{"feed": "alpha"}]),
                Candidate("UNMAPPED", [{"feed": "spot"}])]
    job = SimpleNamespace(account_id="a")
    catalog = CoinCatalog(store, gate, settings, KnownPricingIDResolver({"SPOT": "spot"}), candidates)
    with store.begin() as db:
        db.add(CoinMapping(coin_id="ALPHA", pricing_id="alpha"))
    asyncio.run(catalog.refresh(job))
    assert not fake.calls
    catalog.settings = settings.model_copy(update={"allow_pricing_id_checks": True})
    fake.responses.extend([[balance("entity")], []])
    asyncio.run(catalog.refresh(job))
    asyncio.run(catalog.refresh(job))
    with store() as db:
        assert db.get(Coin, "SPOT").verified
        assert db.get(Coin, "ALPHA").exclusion_reason == "no_balance_data"
        assert db.get(Coin, "UNMAPPED").exclusion_reason == "mapping_required"
    assert len(fake.calls) == 2

    assert all(call[2]["limit"] == "1" for call in fake.calls)


def test_binance_spot_alpha_union():
    feeds = {
        SPOT_URL: {"symbols": [{"status": "TRADING", "baseAsset": "ABC", "quoteAsset": "USD"},
                               {"status": "BREAK", "baseAsset": "NO", "quoteAsset": "USD"}]},
        ALPHA_URL: {"code": "000000", "data": [{"symbol": "abc", "chainId": "1", "contractAddress": "0x1"},
                             {"symbol": "DEF", "chainId": "2", "contractAddress": "0x2"}]},
    }
    transport = MockTransport(lambda request: Response(200, json=feeds[str(request.url)]))
    candidates = asyncio.run(binance_candidates(transport))
    assert {entry.symbol for entry in candidates} == {"ABC", "DEF"}
    assert candidates[0].sources == [{"feed": "spot"}, {"feed": "alpha", "chain_id": "1", "contract_address": "0x1"}]


def test_binance_candidates_skip_symbols_the_api_cannot_address():
    feeds = {
        SPOT_URL: {"symbols": [{"status": "TRADING", "baseAsset": "ABC", "quoteAsset": "USD"}]},
        ALPHA_URL: {"code": "000000", "data": [
            {"symbol": "$PAAL", "chainId": "1", "contractAddress": "0x1"},
            {"symbol": "人生K线", "chainId": "56", "contractAddress": "0x2"},
            {"symbol": "FREEDOM OF MONEY", "chainId": "56", "contractAddress": "0x3"},
        ]},
    }
    transport = MockTransport(lambda request: Response(200, json=feeds[str(request.url)]))
    assert [entry.symbol for entry in asyncio.run(binance_candidates(transport))] == ["ABC"]


def test_api_crud_snapshots_filters_and_socket(settings):
    fake = FakeUpstream()
    fake.responses.extend([{"id": "user"}, [balance("a"), balance("b")], [balance("b", 2), balance("c")]])
    app = create_app(settings, fake, run_scheduler=False)
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        assert client.get("/transfers").status_code == 401
        client.headers.update(headers)
        assert client.put("/accounts/a", json={"proxy_ref": "p"}).status_code == 200
        assert client.get("/accounts/a/session").json() == {"status": "logged_in"}
        assert client.put("/coins/ABC", json={"symbol": "ABC", "pricing_id": "abc", "verified": True}).status_code == 200
        assert client.put("/subscriptions/ABC", json={"enabled": True}).status_code == 200
        assert client.post("/jobs", json={"kind": "transfers", "account_id": "a", "coin_id": "ABC",
                                          "interval_seconds": 60}).status_code == 201
        assert client.post("/jobs", json={"kind": "transfers", "account_id": "a", "coin_id": "ABC",
                                          "interval_seconds": 60}).status_code == 409
        assert client.put("/exclusions/ABC", json={"reason": "test"}).status_code == 200
        assert client.delete("/exclusions/ABC").status_code == 204
        assert client.put("/coins/ABC/mapping", json={"pricing_id": "abc"}).status_code == 200
        assert client.post("/rules", json={"name": "alert", "enabled": True, "source": "transfer", "level": "normal",
                                           "push": True, "conditions": {"min_usd": 200}}).status_code == 201
        for _ in range(2):
            response = client.post("/snapshots", json={"account_id": "a", "pricing_ids": ["abc", "def"]})
            assert response.status_code == 201, response.text
        assert fake.calls[1][2]["pricingIds"] == "abc,def"
        assert len(client.get("/snapshots/1").json()["rows"]) == 2
        comparison = client.get("/snapshots/compare?before=1&after=2").json()
        assert [row["entity_id"] for row in comparison["added"]] == ["c"]
        assert [row["entity_id"] for row in comparison["removed"]] == ["a"]
        assert comparison["changed"][0]["after"]["balance_unit"] == 2
        fake.responses.append({"transfers": [transfer("selected"), transfer("other", 100)]})
        with client.websocket_connect("/ws/events", headers=headers) as ws:
            asyncio.run(app.state.collectors.transfers(SimpleNamespace(coin_id="ABC", account_id="a", interval_seconds=60)))
            event = ws.receive_json()
            assert event["data"]["id"] == "selected"
        params = {"coin_id": "ABC", "min_usd": 300, "max_usd": 600, "chain": "ethereum", "address": "sender",
                  "entity": "e", "direction": "inbound", "txhash": "tx-selected", "start": "2026-01-09T22:00:00-02:00",
                  "end": "2026-01-10T00:00:00Z"}
        rows = client.get("/transfers", params=params).json()
        assert [row["id"] for row in rows] == ["selected"]
        assert rows[0]["time"].endswith("Z") and rows[0]["coin_ids"] == ["ABC"]
        assert client.get("/transfers", params={**params, "offset": 1}).json() == []
        assert client.get("/events", params={"read": "false", "coin_id": "ABC"}).json()[0]["id"] == event["id"]
        assert client.put(f"/events/{event['id']}/read", json={"read": True}).json()["read_at"]
        assert client.get("/events", params={"read": "false"}).json() == []
        fake.responses.append([balance("e")])
        asyncio.run(app.state.collectors.daily(SimpleNamespace(account_id="a", options={"balance_min": 100},
                                                                interval_seconds=3600)))
        curve = client.get("/balances", params=[("coin_id", "ABC"), ("entity_id", "e"), ("entity_id", "missing")]).json()
        assert not curve["days"][0]["truncated"]
        assert curve["series"]["e"][0]["balance_unit"] == 1
        assert curve["series"]["missing"] == []
        with app.state.store() as db:
            assert db.scalar(select(PushLog.event_id)) == event["id"]


def test_openapi_matches_export():
    assert json.loads((Path(__file__).parents[1] / "openapi.json").read_text()) == create_app().openapi()
