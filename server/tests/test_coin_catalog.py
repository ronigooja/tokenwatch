import asyncio
import copy
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from test_core import core, settings, balance
from tokenwatch.api import create_app
from tokenwatch.browser.client import _decode
from tokenwatch.browser.transport import Response
from tokenwatch.coins import ALPHA_URL, ASSET_URL, FUTURES_URL, NETWORK_URL, SPOT_URL, CoinCatalog
from tokenwatch.db import (Account, CatalogCache, CatalogExclusion, ChainObservation, Coin, CoinMapping,
                           DailyCollection, Event, FuturesMapping, Job, PushLog, Snapshot, Transfer, TransferCoin,
                           now, open_store)
from tokenwatch.scheduler import Scheduler
from tokenwatch.upstream import BadResponse


def address(n):
    return f"0x{n:040x}"


def envelope(data):
    return {"code": "000000", "data": data}


def future(base, **kwargs):
    return {"baseAsset": base, "status": "TRADING", "contractType": "PERPETUAL", "underlyingType": "COIN",
            "quoteAsset": "USDT", "marginAsset": "USDT", **kwargs}


def network(chain, contract="", enabled=True):
    return {"network": chain, "contractAddress": contract, "depositEnable": enabled, "withdrawEnable": enabled}


def catalog_feeds():
    tags = {"FIAT": [], "STOCK": ["bStocks", "stablecoin"], "GOLD": ["tCommodities"], "USD": ["stablecoin"],
            "AVAX": ["RWA"], "SHIB": [], "QUARTERLY": [], "COINM": [], "INDEX": [], "UNKNOWN": [], "BTC": []}
    return {
        SPOT_URL: {"symbols": [{"status": "TRADING", "baseAsset": symbol, "isSpotTradingAllowed": True}
                               for symbol in tags] + [{"status": "TRADING", "baseAsset": "NOTSPOT", "isSpotTradingAllowed": False}]},
        ALPHA_URL: envelope([{"symbol": "GAME", "chainId": "1", "contractAddress": address(101)},
                             {"symbol": "GAME", "chainId": "56", "contractAddress": address(102)},
                             {"symbol": "$PAAL", "chainId": "CT_501", "contractAddress": "CaseSensitive"}]),
        NETWORK_URL: envelope([{"coin": symbol, "isLegalMoney": symbol == "FIAT", "networkList": (
            [network("BTC"), network("BSC", address(999))] if symbol == "BTC" else
            [network(symbol, enabled=False)] if symbol == "UNKNOWN" else
            [network("ETH", address(i)), network("BSC", address(i + 200))])} for i, symbol in enumerate(tags, 1)]),
        ASSET_URL: envelope([{"assetCode": symbol, "tags": values, "isLegalMoney": False, "isLedgerOnly": False}
                            for symbol, values in tags.items()]),
        FUTURES_URL: {"symbols": [future(symbol) for symbol in ("FIAT", "STOCK", "GOLD", "USD", "AVAX", "BTC", "GAME")]
                      + [future("QUARTERLY", contractType="CURRENT_QUARTER"), future("COINM", marginAsset="COINM"),
                         future("INDEX", underlyingType="INDEX"), future("1000SHIB")]},
    }


def mock_feeds(feeds, calls):
    def fetch(request):
        url = str(request.url)
        calls.append(url)
        return httpx.Response(200, json=feeds[url])
    return httpx.MockTransport(fetch)


def test_catalog_rules_futures_identity_and_fallback(core):
    store, fake, clock, gate, _ = core
    calls, feeds = [], catalog_feeds()
    catalog = CoinCatalog(store, gate, mock_feeds(feeds, calls), clock)
    with store.begin() as db:
        db.add_all(CatalogExclusion(kind=kind, value=value, reason=reason) for kind, value, reason in (
            ("flag", "isLegalMoney", "fiat"), ("tag", "bStocks", "stock"), ("tag", "tCommodities", "commodity"),
            ("tag", "stablecoin", "stablecoin")))

    def states():
        with store() as db:
            return {row.symbol: (row.status, row.reason) for row in db.scalars(select(Coin))}

    asyncio.run(catalog.refresh_snapshot())
    assert calls == list(feeds)
    first = states()
    assert first["STOCK"] == ("excluded", "stablecoin; stock")
    for symbol, reason in [("FIAT", "fiat"), ("GOLD", "commodity"), ("USD", "stablecoin")]:
        assert first[symbol] == ("excluded", reason)
    assert first["AVAX"][0] == first["GAME"][0] == "pending_match"
    assert first["BTC"][0] == "needs_manual_mapping"
    assert first["UNKNOWN"][0] == "unknown_onchain"
    assert all(first[symbol][0] == "no_futures" for symbol in ("SHIB", "QUARTERLY", "COINM", "INDEX", "$PAAL"))
    assert "NOTSPOT" not in first
    with store.begin() as db:
        assert db.get(Coin, "native:BTC")
        assert len(db.scalars(select(Coin).where(Coin.symbol == "GAME")).all()) == 2
        assert len(db.scalar(select(Coin).where(Coin.symbol == "AVAX")).deployments) == 2
        snapshot = copy.deepcopy(db.get(CatalogCache, "binance").payload)
        db.add(FuturesMapping(base="1000SHIB", symbol="SHIB"))
    del feeds[SPOT_URL]["symbols"][0]["isSpotTradingAllowed"]
    clock.value += 10
    asyncio.run(catalog.refresh_snapshot())
    assert calls == list(feeds) * 2
    assert states()["SHIB"][0] == "pending_match"
    with store() as db:
        cache = db.get(CatalogCache, "binance")
        assert cache.payload == snapshot
        assert cache.refreshed_at == 1000 and cache.attempted_at == 1010
        assert "isSpotTradingAllowed" in cache.error
    assert not fake.calls


def token(pricing_id, chain, contract):
    return {"identifier": {"pricingID": pricing_id, "chain": chain, "address": contract}, "deployments": []}


def search(*tokens):
    return {"bestMatches": [], "tokens": list(tokens)}


def pending(db, id, chain, contract):
    db.add(Coin(id=id, symbol="SAME", sources=["alpha"], deployments=[{"feed": "alpha", "chain": chain, "address": contract,
                                                                    "deposit_enabled": None, "withdraw_enabled": None}],
                status="pending_match"))


def test_lazy_search_support_outcomes_are_persisted(core):
    store, fake, _, gate, _ = core
    catalog = CoinCatalog(store, gate)
    evm = "0x" + "ab" * 20
    with store.begin() as db:
        for id, contract in [("m1", evm), ("m2", "CaseSensitive"), ("m3", "same"), ("m4", "other")]:
            pending(db, id, "1", contract)
        db.add(Coin(id="native:BTC", symbol="BTC", status="pending_support", pricing_id="btc"))
        db.flush()
        db.add(CoinMapping(coin_id="native:BTC", pricing_id="btc"))
    fake.responses.extend([search(token("one", "ethereum", evm.upper())), {"ethereum": "opaque"},
                           search(token("none", "solana", "casesensitive")),
                           search(token("a", "ethereum", "same"), token("b", "base", "same")),
                           search(token("other", "base", "other")),
                           _decode(Response(400, '{"message":"Not supported"}'), 0, "/token/addresses/other"),
                           {"bitcoin": "opaque"}])

    async def run():
        while await catalog.match_next(SimpleNamespace(account_id="a")):
            pass
        assert not await catalog.match_next(SimpleNamespace(account_id="a"))

    asyncio.run(run())
    with store() as db:
        assert db.get(Coin, "m1").matched_chains == ["ethereum"]
        assert [db.get(Coin, id).status for id in ("m1", "m2", "m3", "m4", "native:BTC")] == [
            "supported", "no_match", "ambiguous", "unsupported", "supported"]
        assert set(db.scalars(select(ChainObservation.arkham_chain))) == {"ethereum", "base"}
    assert len(fake.calls) == 7
    assert [params["query"] for _, path, params in fake.calls if path == "/intelligence/search"] == [
        evm, "CaseSensitive", "same", "other"]


@pytest.mark.parametrize("status,path,body,paused", [
    (400, "/token/addresses/id", '{"message":"unsupported"}', False),
    (400, "/token/addresses/id", '{"message":"unsupported","extra":1}', True),
    (400, "/intelligence/search", '{"message":"unexpected"}', True),
    (403, "/token/addresses/id", '{}', True),
])
def test_support_http_exception_is_narrow(core, status, path, body, paused):
    from tokenwatch.coins import support_status
    store, fake, clock, gate, _ = core
    try:
        payload = _decode(Response(status, body), clock(), path)
    except BadResponse as exc:
        payload = exc
    fake.responses.append(payload)
    if paused:
        with pytest.raises(BadResponse):
            asyncio.run(gate.get("a", path, None, support_status))
    else:
        assert asyncio.run(gate.get("a", path, None, support_status)) == "unsupported"
    with store() as db:
        account = db.get(Account, "a")
        assert (account.status, account.failures) == (("paused", 1) if paused else ("active", 0))
        assert account.available_at == pytest.approx(1000 + (20 if paused else 0.001))


def test_distributed_selection_failover_and_wait(core, settings):
    from tokenwatch.upstream import Blocked
    store, fake, clock, gate, _ = core
    with store.begin() as db:
        db.get(Account, "a").available_at = 900
        db.get(Account, "b").available_at = 800
        db.add(Account(id="paused", proxy_ref="paused", status="paused"))
        db.add(Job(kind="daily", account_id=None, enabled=True, interval_seconds=30))
    for id in ("a", "b", "paused"):
        gate.remember_status(id, "logged_in")
    fake.responses.extend([Blocked(), {"ok": True}])

    async def handler(job):
        await gate.get(job.account_id, "/example", None)
        return clock()

    scheduler = Scheduler(store, settings, {"daily": handler}, clock)

    async def run():
        scheduler.tick()
        await scheduler.running[1]
        assert [call[0] for call in fake.calls] == ["b", "a"]
        scheduler.tick()
        await scheduler.running[1]
        with store.begin() as db:
            job = db.get(Job, 1)
            assert job.wait_reason == "waiting_for_account_request_gate"
            clock.value = job.next_run
            db.get(Account, "a").status = "paused"
        scheduler.tick()
        await scheduler.running[1]
        with store() as db:
            assert db.get(Job, 1).wait_reason == "no_active_account_with_confirmed_session"
        assert len(fake.calls) == 2

    asyncio.run(run())


def test_distributed_ties_use_least_recently_used(core):
    store, fake, _, gate, _ = core
    with store.begin() as db:
        db.get(Account, "a").last_used_at = 100
        db.get(Account, "b").last_used_at = 50
    gate.remember_status("a", "logged_in")
    gate.remember_status("b", "logged_in")
    fake.responses.append({})
    asyncio.run(gate.get(None, "/example", None))
    assert fake.calls[0][0] == "b"


def test_daily_matches_lazily_and_collects_every_supported_coin(core):
    store, fake, _, gate, collectors = core
    collectors.catalog = CoinCatalog(store, gate)
    with store.begin() as db:
        pending(db, "alpha:1:Addr", "1", "Addr")
        db.add(Coin(id="unsupported", symbol="NO", status="unsupported", pricing_id="no"))
    fake.responses.extend([[balance("a")], search(token("token", "ethereum", "Addr")),
                           {"ethereum": "opaque"}, [balance(str(i)) for i in range(50)]])

    async def run():
        job = SimpleNamespace(account_id="a", options={"balance_min": 200}, interval_seconds=3600)
        for _ in range(4):
            await collectors.daily(job)

    asyncio.run(run())
    with store() as db:
        assert {row.coin_id: row.truncated for row in db.scalars(select(DailyCollection))} == {
            "ABC": False, "alpha:1:Addr": True,
        }
    for _, path, params in fake.calls:
        if path.endswith("entity_balance_changes"):
            assert (params["limit"], params["offset"], params["balanceMin"]) == ("50", "0", "200")


def test_api_accountless_catalog_rules_and_native_override(settings):
    app = create_app(settings, run_scheduler=False)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        assert client.get("/coin-catalog").json()["status"] == "unknown"
        with app.state.store.begin() as db:
            db.add(Coin(id="native:BTC", symbol="BTC", sources=["spot"], status="needs_manual_mapping"))
        job = {"kind": "daily", "interval_seconds": 60, "options": {"balance_min": 100}, "account_id": None}
        assert client.post("/jobs", json=job).json()["account_id"] is None
        assert client.put("/jobs/1", json=job).status_code == 200
        assert client.put("/coins/native:BTC/mapping", json={"pricing_id": "bitcoin"}).status_code == 200
        assert client.get("/coins", params={"status": "pending_support"}).json()[0]["id"] == "native:BTC"
        assert client.put("/coin-catalog/futures-mappings/1000SHIB", json={"symbol": "SHIB"}).status_code == 200
        assert client.put("/coin-catalog/exclusion-rules/tag/stablecoin", json={"reason": "stablecoin"}).status_code == 200
        assert client.get("/coin-catalog/exclusion-rules").json()[0]["kind"] == "tag"


def test_schema_reset_preserves_history_and_jobs(settings):
    store = open_store(settings.database)
    with store.begin() as db:
        db.add_all([Account(id="a", proxy_ref="p"), Coin(id="OLD", symbol="OLD"),
                    Transfer(id="tx", direction="unknown", time=now(), gap=False),
                    Snapshot(interval="7d", truncated=False),
                    Event(dedupe_key="e", kind="alarm", coin_id="OLD", level="normal", push=True, data={})])
        db.flush()
        db.add_all([TransferCoin(transfer_id="tx", coin_id="OLD"), PushLog(event_id=1),
                    Job(kind="transfers", account_id="a", coin_id="OLD", enabled=True, interval_seconds=60),
                    Job(kind="daily", account_id="a", enabled=True, interval_seconds=60),
                    DailyCollection(day=now().date(), coin_id="OLD", truncated=False)])
    with store.begin() as db:
        db.execute(text("ALTER TABLE accounts DROP COLUMN last_used_at"))
        db.execute(text("PRAGMA user_version=1"))
    store.kw["bind"].dispose()
    migrated = open_store(settings.database)
    with migrated() as db:
        assert db.get(Coin, "OLD") is None
        assert db.query(DailyCollection).count() == 0
        assert db.get(TransferCoin, ("tx", "OLD")) and db.get(Transfer, "tx")
        assert db.get(Event, 1).coin_id == "OLD" and db.get(PushLog, 1).event_id == 1
        assert db.get(Snapshot, 1) and db.get(Account, "a")
        assert not db.get(Job, 1).enabled and db.get(Job, 1).wait_reason == "coin_reassignment_required"
        assert db.get(Job, 2).enabled
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
        assert db.scalar(text("PRAGMA foreign_keys")) == 1
    migrated.kw["bind"].dispose()
