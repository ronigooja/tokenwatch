import asyncio
import copy
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from test_core import balance, core, enable_snapshot_job, settings
from tokenwatch.api import create_app
from tokenwatch.browser.client import _decode
from tokenwatch.browser.transport import Response
from tokenwatch.coins import ALPHA_URL, ASSET_URL, FUTURES_URL, NETWORK_URL, SPOT_URL, TICKER_URL, CoinCatalog
from tokenwatch.db import (Account, CatalogCache, CatalogExclusion, CatalogSettings, ChainObservation, Coin, CoinMapping,
                           DailyCollection, Event, FuturesMapping, Job, PushLog, Snapshot, Transfer, TransferCoin,
                           now, open_store)
from tokenwatch.scheduler import Scheduler
from tokenwatch.upstream import BadResponse


def address(n):
    return f"0x{n:040x}"


@pytest.mark.parametrize("configured", [False, True])
def test_catalog_proxy_and_sanitized_failure(core, settings, monkeypatch, caplog, configured):
    from pydantic import SecretStr
    from tokenwatch import coins
    store, fake, clock, gate, _ = core
    proxy = "socks5h://user:password@proxy.invalid:1234"
    settings.binance_proxy_url = SecretStr(proxy) if configured else None
    seen, calls = [], []
    original = httpx.AsyncClient

    def client(**kwargs):
        seen.append(kwargs.pop("proxy"))
        return original(**kwargs)

    def response(request):
        calls.append(request)
        return httpx.Response(451, text=proxy + "?secret=hidden")

    monkeypatch.setattr(coins.httpx, "AsyncClient", client)
    catalog = CoinCatalog(store, gate, httpx.MockTransport(response), clock, settings)
    with store.begin() as db:
        db.add(CatalogSettings(key="matching", failure_limit=3, refresh_retry_seconds=42))
        job = Job(kind="coin_catalog", account_id=None, enabled=True, interval_seconds=604800)
        db.add(job)
        db.flush()
        job_id = job.id
    scheduler = Scheduler(store, settings, {"coin_catalog": catalog.refresh}, clock, gate)
    asyncio.run(scheduler.run_job(job))
    with store() as db:
        error = db.get(CatalogCache, "binance").error
        saved = db.get(Job, job_id)
        assert saved.next_run == clock() + 42
        assert saved.last_error == error
    assert seen == [proxy if configured else None]
    assert len(calls) == 1 and not fake.calls
    assert "spot exchangeInfo" in error and "451" in error and "HTTPStatusError" in error
    assert len(error) <= 200 and proxy not in error and proxy not in caplog.text
    with TestClient(create_app(settings, run_scheduler=False)) as api:
        result = api.get("/coin-catalog", headers={"Authorization": "Bearer test-token"})
        assert proxy not in result.text and result.json()["error"] == error


def test_catalog_5xx_retry_and_missing_setting(core, settings, monkeypatch):
    from tokenwatch import coins
    store, _, clock, gate, _ = core
    calls, sleeps = [], []
    async def sleep(delay):
        sleeps.append(delay)
    original = coins.public_json
    async def fast(client, url):
        return await original(client, url, sleep)
    monkeypatch.setattr(coins, "public_json", fast)
    def response(request):
        calls.append(request)
        return httpx.Response(500)
    catalog = CoinCatalog(store, gate, httpx.MockTransport(response), clock, settings)
    with pytest.raises(coins.MissingCatalogRefreshRetry):
        asyncio.run(catalog.refresh(SimpleNamespace(wait_reason=None)))
    assert not calls
    error = asyncio.run(catalog.refresh_snapshot())
    assert len(calls) == 3 and sleeps == [30, 60] and "500" in error


@pytest.mark.parametrize("proxy", ["ftp://secret@host:1", "socks5h://secret@:123", "http://secret@host", "https://secret@host:99999"])
def test_invalid_catalog_proxy_is_redacted(settings, proxy):
    from pydantic import ValidationError
    from tokenwatch.config import Settings
    values = settings.model_dump()
    values["binance_proxy_url"] = proxy
    with pytest.raises(ValidationError) as error:
        Settings.model_validate(values)
    assert proxy not in str(error.value) and "Invalid Binance proxy URL" in str(error.value)


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
        return httpx.Response(200, json=[] if url == TICKER_URL else feeds[url])
    return httpx.MockTransport(fetch)


def test_manual_mapping_reprojects_network_evidence(core, settings):
    store, fake, clock, gate, _ = core
    feeds, calls = catalog_feeds(), []
    feeds[FUTURES_URL]["symbols"].append(future("UNKNOWN"))
    catalog = CoinCatalog(store, gate, mock_feeds(feeds, calls), clock)
    asyncio.run(catalog.refresh_snapshot())
    coin_id = "unknown:spot:UNKNOWN"
    app = create_app(settings, run_scheduler=False)
    with TestClient(app) as client:
        client.headers.update({"Authorization": "Bearer test-token"})
        def status():
            return client.get(f"/coins/{coin_id}").json()["status"]

        assert status() == "unknown_onchain"
        assert client.get("/coins/native:BTC").json()["status"] == "needs_manual_mapping"
        before = len(calls)
        assert client.put(f"/coins/{coin_id}/mapping", json={"pricing_id": "unknown"}).status_code == 200
        assert status() == "pending_support"
        assert len(calls) == before and not fake.calls
        asyncio.run(catalog.refresh_snapshot())
        assert status() == "pending_support"
        with store.begin() as db:
            db.add(CatalogExclusion(kind="tag", value="blocked", reason="blocked"))
        feeds[ASSET_URL]["data"][-2]["tags"] = ["blocked"]
        asyncio.run(catalog.refresh_snapshot())
        assert status() == "excluded"
        feeds[ASSET_URL]["data"][-2]["tags"] = []
        feeds[FUTURES_URL]["symbols"] = [row for row in feeds[FUTURES_URL]["symbols"] if row["baseAsset"] != "UNKNOWN"]
        asyncio.run(catalog.refresh_snapshot())
        assert status() == "no_futures"
        feeds[FUTURES_URL]["symbols"].append(future("UNKNOWN"))
        asyncio.run(catalog.refresh_snapshot())
        assert client.delete(f"/coins/{coin_id}/mapping").status_code == 204
        assert status() == "unknown_onchain"


def test_optional_network_fields_and_empty_tags():
    from tokenwatch.coins import candidates, classify, parse_feeds
    feeds = catalog_feeds()
    rows = feeds[NETWORK_URL]["data"]
    rows[-1]["networkList"] = [{"network": "BTC", "contractAddress": None, "depositEnable": True}]
    rows[-2]["networkList"] = [{"network": "BTC", "contractAddress": None, "depositEnable": True}]
    feeds[ASSET_URL]["data"][-1]["tags"] = [""]
    snapshot = parse_feeds(*feeds.values())
    coins = {row.symbol: row for row in candidates(snapshot)}
    assert coins["BTC"].id == "native:BTC"
    assert coins["BTC"].deployments[0]["withdraw_enabled"] is None
    assert snapshot["assets"]["BTC"]["tags"] == []
    assert coins["UNKNOWN"].id == "unknown:spot:UNKNOWN"
    assert classify(coins["UNKNOWN"], snapshot, set(), [])[0] == "unknown_onchain"


@pytest.mark.parametrize("bad", [{}, {"network": None, "contractAddress": "address"},
                                {"network": "BTC", "contractAddress": 1, "depositEnable": True}])
def test_malformed_network_is_retained_without_inventing_identity(bad):
    from tokenwatch.coins import candidates, classify, parse_feeds
    feeds = catalog_feeds()
    feeds[NETWORK_URL]["data"][-1]["networkList"] = [bad]
    snapshot = parse_feeds(*feeds.values())
    coin = next(row for row in candidates(snapshot) if row.symbol == "BTC")
    assert coin.id == "unknown:spot:BTC"
    assert len(coin.deployments) == 1
    assert classify(coin, snapshot, {"BTC"}, [])[0] == "unknown_onchain"


@pytest.mark.parametrize("url,bad", [(SPOT_URL, {}), (NETWORK_URL, {"code": "000000"}),
                                    (ASSET_URL, {"code": "error", "data": []})])
def test_top_level_failure_preserves_snapshot(core, url, bad):
    store, _, clock, gate, _ = core
    feeds = catalog_feeds()
    catalog = CoinCatalog(store, gate, mock_feeds(feeds, []), clock)
    asyncio.run(catalog.refresh_snapshot())
    with store() as db:
        snapshot = copy.deepcopy(db.get(CatalogCache, "binance").payload)
    feeds[url] = bad
    clock.value += 1
    asyncio.run(catalog.refresh_snapshot())
    with store() as db:
        cache = db.get(CatalogCache, "binance")
        assert cache.payload == snapshot and cache.refreshed_at == 1000 and cache.error


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
    assert calls == [*feeds, TICKER_URL]
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
    assert calls == [*feeds, TICKER_URL] * 2
    assert states()["SHIB"][0] == "pending_match"
    with store() as db:
        cache = db.get(CatalogCache, "binance")
        assert cache.payload == snapshot
        assert cache.refreshed_at == 1000 and cache.attempted_at == 1010
        assert "ValueError" in cache.error
    assert not fake.calls


def token(pricing_id, chain, contract):
    return {"identifier": {"pricingID": pricing_id, "chain": chain, "address": contract}, "deployments": []}


def search(*tokens):
    return {"bestMatches": [], "tokens": list(tokens)}


def configure_matching(settings):
    with TestClient(create_app(settings, run_scheduler=False)) as client:
        headers = {"Authorization": "Bearer test-token"}
        assert client.put("/coin-catalog/settings", json={"failure_limit": 3}, headers=headers).status_code == 422
        values = {"failure_limit": 3, "refresh_retry_seconds": 60, "price_tolerance_pct": None, "price_max_age_seconds": None}
        assert client.put("/coin-catalog/settings", json=values, headers=headers).json() == values
        assert client.get("/coin-catalog/settings", headers=headers).json() == values


def test_search_matches_realistic_responses():
    from tokenwatch.coins import search_matches
    contract = address(42)
    payload = {"bestMatches": [], "arkhamEntities": [{"name": "ignored"}], "tokens": [
        {"identifier": {"pricingID": "uniswap"}, "name": "Uniswap", "symbol": "UNI",
         "price": None, "price24hAgo": None, "deployments": [
             {"chain": "ethereum", "address": contract}, {"chain": "base", "address": contract}]},
        {"identifier": {"pricingID": "other", "chain": "ethereum", "address": contract},
         "name": "Other", "symbol": "OTH", "deployments": []},
        {"identifier": None, "deployments": [{"chain": "base", "address": contract}]},
        {"identifier": {"pricingID": "broken"}, "deployments": [{"chain": None, "address": contract}, {}]},
    ]}
    assert search_matches(payload, contract) == {"uniswap": {"ethereum", "base"}, "other": {"ethereum"}}
    assert search_matches({"bestMatches": [{}], "tokens": [payload["tokens"][1]]}, contract) == {
        "other": {"ethereum"}}
    assert not search_matches({"bestMatches": []}, contract)


@pytest.mark.parametrize("price,reference,age,configured,expected", [
    (10.5, 10, 0, True, "ok"), (12, 10, 0, True, "mismatch"),
    (10, None, 0, True, "no_reference"), (None, 10, 0, True, "no_reference"), (10, 10, 61, True, "stale"),
    (10, 10, 0, False, None),
])
def test_automatic_price_check(core, settings, price, reference, age, configured, expected):
    store, fake, clock, gate, _ = core
    feeds = catalog_feeds()
    feeds[SPOT_URL]["symbols"] = [feeds[SPOT_URL]["symbols"][-1]]
    feeds[ALPHA_URL] = envelope([{"symbol": "GAME", "chainId": "1", "contractAddress": address(101)}])
    def respond(request):
        if str(request.url) == TICKER_URL:
            return httpx.Response(200, json=[] if reference is None else [{"symbol": "GAMEUSDT", "price": str(reference)}])
        return httpx.Response(200, json=feeds[str(request.url)])
    catalog = CoinCatalog(store, gate, httpx.MockTransport(respond), clock)
    asyncio.run(catalog.refresh_snapshot())
    with store.begin() as db:
        cache = db.get(CatalogCache, "binance")
        cache.payload = {**cache.payload, "prices_at": clock() - age}
        db.add(CatalogSettings(key="matching", failure_limit=3, refresh_retry_seconds=60,
                               price_tolerance_pct=10 if configured else None,
                               price_max_age_seconds=60 if configured else None))
    item = token("game", "1", address(101))
    item["price"] = price
    fake.responses.append(search(item))
    asyncio.run(catalog.match_next(SimpleNamespace(account_id="a")))
    coin_id = f"alpha:1:{address(101)}"
    with store() as db:
        coin = db.get(Coin, coin_id)
        assert coin.price_check_status == expected
        assert coin.status == "pending_support"
        if not configured:
            assert coin.price_check_reason == "price settings not configured"
    fake.responses.append({"ethereum": "address"})
    asyncio.run(catalog.match_next(SimpleNamespace(account_id="a")))
    with store() as db:
        assert db.get(Coin, coin_id).status == "supported"
        assert db.get(Coin, coin_id).price_check_status == expected
    with store.begin() as db:
        db.get(CatalogSettings, "matching").price_tolerance_pct = 0
    asyncio.run(catalog.refresh_snapshot())
    with store() as db:
        assert db.get(Coin, coin_id).price_check_status == expected


def test_manual_price_check_and_clear(core, settings):
    store, fake, clock, gate, _ = core
    feeds, calls = catalog_feeds(), []
    feeds[ALPHA_URL] = envelope([{"symbol": "GAME", "chainId": "1", "contractAddress": address(101)}])
    catalog = CoinCatalog(store, gate, mock_feeds(feeds, calls), clock)
    asyncio.run(catalog.refresh_snapshot())
    with store.begin() as db:
        db.add(CatalogSettings(key="matching", failure_limit=3, refresh_retry_seconds=60,
                               price_tolerance_pct=10, price_max_age_seconds=60))
    item = token("game", "ethereum", address(101))
    item["price"] = 20
    fake.responses.append(search(item))
    asyncio.run(catalog.match_next(SimpleNamespace(account_id="a")))
    coin_id = f"alpha:1:{address(101)}"
    with TestClient(create_app(settings, run_scheduler=False)) as client:
        client.headers.update({"Authorization": "Bearer test-token"})
        before = (len(calls), len(fake.calls))
        assert client.put(f"/coins/{coin_id}/mapping", json={"pricing_id": "manual"}).status_code == 200
        checked = client.get(f"/coins/{coin_id}").json()
        assert checked["price_check_status"] == "manual"
        assert client.post(f"/coins/{coin_id}/price-check").status_code in (404, 405)
        assert any(row["id"] == coin_id for row in client.get("/coins?price_check_status=manual").json())
        assert client.delete(f"/coins/{coin_id}/mapping").status_code == 204
        restored = client.get(f"/coins/{coin_id}").json()
        assert all(restored[key] is None for key in ("price_check_status", "price_check_ratio", "price_checked_at", "price_check_reason"))
        assert (len(calls), len(fake.calls)) == before


def test_ticker_failure_preserves_prices(core):
    store, _, clock, gate, _ = core
    feeds = catalog_feeds()
    fail = False
    def respond(request):
        if str(request.url) == TICKER_URL:
            return httpx.Response(503) if fail else httpx.Response(200, json=[{"symbol": "GAMEUSDT", "price": "2"}])
        return httpx.Response(200, json=feeds[str(request.url)])
    catalog = CoinCatalog(store, gate, httpx.MockTransport(respond), clock)
    assert asyncio.run(catalog.refresh_snapshot()) is None
    clock.value += 100
    fail = True
    assert asyncio.run(catalog.refresh_snapshot()) is None
    with store() as db:
        cache = db.get(CatalogCache, "binance")
        assert cache.payload["prices"] == {"GAME": 2}
        assert cache.payload["prices_at"] == clock() - 100


@pytest.mark.parametrize("payload,expected", [
    ({"twitter": []}, "no_match"),
    ({"tokens": None}, "no_match"),
    ({"tokens": []}, "no_match"),
    ({"tokens": "x"}, "error"),
    ([], "error"),
    (search(token("game", "ethereum", address(101))), "pending_support"),
])
def test_classified_search_payload_through_scheduler(core, settings, payload, expected):
    store, fake, clock, gate, _ = core
    configure_matching(settings)
    feeds = catalog_feeds()
    feeds[ALPHA_URL] = envelope([{"symbol": "GAME", "chainId": "1", "contractAddress": address(101)}])
    catalog = CoinCatalog(store, gate, mock_feeds(feeds, []), clock)
    asyncio.run(catalog.refresh_snapshot())
    coin_id = f"alpha:1:{address(101)}"
    with store.begin() as db:
        assert db.get(Coin, coin_id).status == "pending_match"
        db.add(Job(kind="coin_catalog", account_id="a", enabled=True, interval_seconds=60))
    fake.responses.append(payload)

    async def handler(job):
        await catalog.match_next(job)

    scheduler = Scheduler(store, settings, {"coin_catalog": handler}, clock)
    with store() as db:
        job = db.scalar(select(Job).where(Job.kind == "coin_catalog"))
    asyncio.run(scheduler.run_job(job))
    with store() as db:
        coin, account, job = db.get(Coin, coin_id), db.get(Account, "a"), db.get(Job, job.id)
        if expected == "error":
            assert coin.status == "pending_match"
            assert (account.failures, job.last_error) == (1, "BadResponse on /intelligence/search")
        else:
            assert (coin.status, account.failures, job.last_error) == (expected, 0, None)
            assert account.status == "active"
            if expected == "no_match":
                assert coin.reason == "no Arkham token matches the contract"
            else:
                assert (coin.pricing_id, coin.matched_chains) == ("game", ["ethereum"])
    assert fake.calls == [("a", "/intelligence/search", {"query": address(101)})]


def pending(db, id, chain, contract):
    db.add(Coin(id=id, symbol="SAME", sources=["alpha"], deployments=[{"feed": "alpha", "chain": chain, "address": contract,
                                                                    "deposit_enabled": None, "withdraw_enabled": None}],
                status="pending_match"))


@pytest.mark.parametrize("change", ["disable", "account", "delete", "options", "interval", "kind", "coin", "closing", "unchanged"])
def test_catalog_pass_rechecks_job_and_resumes(core, settings, change):
    store, fake, clock, gate, _ = core
    with store.begin() as db:
        db.add(CatalogSettings(key="matching", failure_limit=3, refresh_retry_seconds=60))
        for i in range(3):
            pending(db, f"p{i}", "1", f"contract{i}")
        job = Job(kind="coin_catalog", account_id="a", enabled=True, interval_seconds=604800,
                  wait_reason="retry", last_error="previous")
        db.add(job)
        db.flush()
        job_id = job.id

    entered, release = asyncio.Event(), asyncio.Event()

    async def upstream(account_id, path, params):
        fake.calls.append((account_id, path, params))
        if len(fake.calls) == 1:
            entered.set()
            await release.wait()
        return search()

    fake.get_json = upstream
    catalog = CoinCatalog(store, gate, transport=mock_feeds(catalog_feeds(), []),
                          clock=clock)
    # This test isolates job rechecks during matching; do not add catalog coins on resume.
    async def refresh_snapshot():
        return None
    catalog.refresh_snapshot = refresh_snapshot
    scheduler = Scheduler(store, settings, {"coin_catalog": catalog.refresh}, clock, gate=gate)
    app = create_app(settings, run_scheduler=False)
    app.state.store, app.state.settings = store, settings
    body = {"kind": "coin_catalog", "account_id": "a", "coin_id": None, "options": {},
            "interval_seconds": 604800, "enabled": True}

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     headers={"Authorization": "Bearer test-token"}) as client:
            scheduler.tick()
            task = scheduler.running["a"]
            await asyncio.wait_for(entered.wait(), 2)
            if change == "delete":
                assert (await client.delete(f"/jobs/{job_id}")).status_code == 204
            elif change == "closing":
                gate.closing = True
            elif change in ("options", "coin"):
                # These configurations are rejected by the public API but may exist in the store.
                with store.begin() as db:
                    current = db.get(Job, job_id)
                    if change == "options":
                        current.options = {"test": 1}
                    else:
                        current.coin_id = "ABC"
            elif change != "unchanged":
                updates = {"disable": {"enabled": False}, "account": {"account_id": "b"},
                           "interval": {"interval_seconds": 60},
                           "kind": {"kind": "daily", "options": {"balance_min": 1}}}
                response = await client.put(f"/jobs/{job_id}", json={**body, **updates[change]})
                assert response.status_code == 200, response.text
            release.set()
            await asyncio.wait_for(task, 2)
            assert len(fake.calls) == (3 if change == "unchanged" else 1)
            with store() as db:
                assert [db.get(Coin, f"p{i}").status for i in range(3)] == (
                    ["no_match"] * 3 if change == "unchanged" else ["no_match", "pending_match", "pending_match"])
                if change == "disable":
                    saved = db.get(Job, job_id)
                    assert (saved.next_run, saved.last_error) == (0, "previous")
            if change == "disable":
                response = await client.put(f"/jobs/{job_id}", json=body)
                assert response.status_code == 200 and response.json()["next_run"] == 0
                clock.value += settings.request_interval
                scheduler.tick()
                await asyncio.wait_for(scheduler.running["a"], 2)
                assert [call[2]["query"] for call in fake.calls] == ["contract0", "contract1", "contract2"]
                with store() as db:
                    assert all(db.get(Coin, f"p{i}").status == "no_match" for i in range(3))
                    assert db.get(Job, job_id).last_error is None

    asyncio.run(run())


def test_failure_rotation_limit_and_manual_retry(core, settings):
    store, fake, clock, gate, _ = core
    catalog = CoinCatalog(store, gate, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})), clock=clock)
    with store.begin() as db:
        pending(db, "a1", "1", "bad")
        pending(db, "a2", "1", "good")
    job = SimpleNamespace(account_id="a")
    with pytest.raises(Exception, match="MissingCatalogFailureLimit"):
        # The job error is the exception class, not its message.
        try:
            asyncio.run(catalog.match_next(job))
        except Exception as exc:
            raise RuntimeError(type(exc).__name__) from exc
    assert not fake.calls
    configure_matching(settings)
    fake.responses.extend([BadResponse("secret"), search(), BadResponse("secret"), BadResponse("secret")])
    for _ in range(4):
        try:
            asyncio.run(catalog.match_next(job))
        except BadResponse:
            pass
    with store() as db:
        bad, good = db.get(Coin, "a1"), db.get(Coin, "a2")
        assert (bad.status, bad.match_failures, bad.reason) == ("match_failed", 3, "BadResponse: 3 consecutive failures")
        assert good.status == "no_match"
    assert [call[2]["query"] for call in fake.calls] == ["bad", "good", "bad", "bad"]
    with TestClient(create_app(settings, run_scheduler=False), headers={"Authorization": "Bearer test-token"}) as client:
        result = client.post("/coins/a1/retry")
        assert result.status_code == 200
        assert (result.json()["status"], result.json()["match_failures"]) == ("pending_match", 0)


def test_any_account_failure_retries_at_gate_and_restart(core, settings):
    store, fake, clock, gate, _ = core
    configure_matching(settings)
    catalog = CoinCatalog(store, gate, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})), clock=clock)
    with store.begin() as db:
        pending(db, "a1", "1", "bad")
        db.get(Account, "b").available_at = 1050
        job = Job(kind="coin_catalog", account_id=None, enabled=True, interval_seconds=604800)
        db.add(job)
        db.flush()
        job_id = job.id
    fake.responses.extend([BadResponse(), search()])
    scheduler = Scheduler(store, settings, {"coin_catalog": catalog.match_next}, clock, gate=gate)
    with store() as db:
        job = db.get(Job, job_id)
    asyncio.run(scheduler.run_job(job))
    with store() as db:
        job = db.get(Job, job_id)
        assert job.next_run == 1020
        assert job.wait_reason == "waiting_for_account_request_gate"
    clock.value = 1020
    scheduler = Scheduler(store, settings, {"coin_catalog": catalog.match_next}, clock, gate=gate)
    asyncio.run(scheduler.run_job(job))
    with store() as db:
        assert db.get(Coin, "a1").status == "no_match"
        assert db.get(Coin, "a1").match_failures == 0


def test_lazy_search_support_outcomes_are_persisted(core, settings):
    store, fake, _, gate, _ = core
    configure_matching(settings)
    catalog = CoinCatalog(store, gate, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    evm = "0x" + "ab" * 20
    with store.begin() as db:
        for id, contract in [("m1", evm), ("m2", "CaseSensitive"), ("m3", "same"), ("m4", "other")]:
            pending(db, id, "1", contract)
        db.add(Coin(id="native:BTC", symbol="BTC", status="pending_support", pricing_id="btc"))
        db.flush()
        db.add(CoinMapping(coin_id="native:BTC", pricing_id="btc"))
    fake.responses.extend([search(token("one", "ethereum", evm.upper())),
                           search(token("none", "solana", "casesensitive")),
                           search(token("a", "ethereum", "same"), token("b", "base", "same")),
                           search(token("other", "base", "other")),
                           {"bitcoin": "opaque"}, {"ethereum": "opaque"},
                           _decode(Response(400, '{"message":"Not supported"}'), 0, "/token/addresses/other")])

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


@pytest.mark.parametrize("payload,shape,expected", [
    ({}, None, "supported"),
    ([], "type=list keys=0 str=0 int=0 float=0 bool=0 null=0 list=0 dict=0 message=0 empty_keys=0 max_str=none", "error"),
    ({"ethereum": ["0xSECRET"]}, "type=dict keys=1 str=0 int=0 float=0 bool=0 null=0 list=1 dict=0 message=0 empty_keys=0 max_str=none", "error"),
    ({"ethereum": {"secretkey": 1}}, "type=dict keys=1 str=0 int=0 float=0 bool=0 null=0 list=0 dict=1 message=0 empty_keys=0 max_str=none", "error"),
    ({"message": "secretvalue", "ethereum": "0xSECRET"}, "type=dict keys=2 str=2 int=0 float=0 bool=0 null=0 list=0 dict=0 message=1 empty_keys=0 max_str=<=64", "error"),
    ({"ethereum": "0xSECRET"}, None, "supported"),
    (_decode(Response(400, '{"message":"secretvalue"}'), 0, "/token/addresses/id"), None, "unsupported"),
])
def test_support_shape_through_scheduler(core, settings, caplog, payload, shape, expected):
    store, fake, clock, gate, _ = core
    configure_matching(settings)
    catalog = CoinCatalog(store, gate, transport=mock_feeds(catalog_feeds(), []), clock=clock)
    with store.begin() as db:
        db.add(Coin(id="native:TEST", symbol="TEST", status="pending_support", pricing_id="test"))
        db.flush()
        db.add(CoinMapping(coin_id="native:TEST", pricing_id="test"))
        job = Job(kind="coin_catalog", account_id="a", enabled=True, interval_seconds=60)
        db.add(job)
        db.flush()
        job_id = job.id
    fake.responses.append(payload)
    scheduler = Scheduler(store, settings, {"coin_catalog": catalog.match_next}, clock, gate)
    with store() as db:
        job = db.get(Job, job_id)
    asyncio.run(scheduler.run_job(job))
    with store() as db:
        coin, account, job = db.get(Coin, "native:TEST"), db.get(Account, "a"), db.get(Job, job_id)
        assert account.status == "active"
        if expected == "error":
            assert (coin.status, coin.match_failures, account.failures) == ("pending_support", 1, 1)
            assert account.available_at == clock() + settings.backoff_initial
            assert job.last_error == f"BadResponse[{shape}] on /token/addresses/"
            assert len(job.last_error) <= 200
            assert f"exception=BadResponse status=None shape={shape}" in caplog.text
            assert all(secret not in caplog.text + job.last_error for secret in ("ethereum", "0xSECRET", "secretvalue", "secretkey"))
        else:
            assert (coin.status, coin.match_failures, account.failures, job.last_error) == (expected, 0, 0, None)
    assert fake.calls == [("a", "/token/addresses/test", None)]


def test_support_shape_counts_and_buckets():
    from tokenwatch.coins import support_status
    payload = {"": "a" * 257, "i": 1, "f": 1.5, "b": True, "n": None, "l": [], "d": {}}
    with pytest.raises(BadResponse) as error:
        support_status(payload)
    assert str(error.value) == ("invalid token addresses payload: type=dict keys=7 str=1 int=1 float=1 "
                                "bool=1 null=1 list=1 dict=1 message=0 empty_keys=1 max_str=>256")
    with pytest.raises(BadResponse) as error:
        support_status({str(i): "x" * 65 for i in range(1001)} | {"message": "secret"})
    assert "keys=999 str=999" in str(error.value) and "max_str=<=256" in str(error.value)
    assert len(str(error.value)) <= 200


def test_empty_support_response_resets_account_failures(core):
    from tokenwatch.coins import support_status
    store, fake, clock, gate, _ = core
    with store.begin() as db:
        db.get(Account, "a").failures = 2
    fake.responses.append({})
    assert asyncio.run(gate.get("a", "/token/addresses/native", None, support_status)) == "supported"
    with store() as db:
        account = db.get(Account, "a")
        assert (account.status, account.failures) == ("active", 0)
    assert fake.calls == [("a", "/token/addresses/native", None)]


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
    with store() as db:
        job_id = db.scalar(select(Job.id).where(Job.kind == "daily"))

    async def run():
        scheduler.tick()
        await scheduler.running[job_id]
        assert [call[0] for call in fake.calls] == ["b", "a"]
        scheduler.tick()
        await scheduler.running[job_id]
        with store.begin() as db:
            job = db.get(Job, job_id)
            assert job.wait_reason == "waiting_for_account_request_gate"
            clock.value = job.next_run
            db.get(Account, "a").status = "paused"
        scheduler.tick()
        await scheduler.running[job_id]
        with store() as db:
            assert db.get(Job, job_id).wait_reason == "no_eligible_account"
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


def test_distributed_empty_cache_selects_earliest_active_account(core):
    store, fake, _, gate, _ = core
    with store.begin() as db:
        db.get(Account, "a").available_at = 900
        db.get(Account, "b").available_at = 800
        db.add(Account(id="paused", proxy_ref="paused", status="paused", available_at=700))
    assert not gate.status_cache
    fake.responses.append({})
    asyncio.run(gate.get(None, "/example", None))
    assert fake.calls == [("b", "/example", None)]


def test_distributed_skips_logged_out_until_status_changes(core):
    from tokenwatch.scheduler import DistributionWait

    store, fake, _, gate, _ = core
    with store.begin() as db:
        db.get(Account, "a").available_at = 800
        db.get(Account, "b").available_at = 900
    gate.remember_status("a", "logged_out")
    fake.responses.append({})
    asyncio.run(gate.get(None, "/example", None))
    assert [call[0] for call in fake.calls] == ["b"]
    gate.remember_status("b", "logged_out")
    with pytest.raises(DistributionWait, match="no_eligible_account"):
        asyncio.run(gate.get(None, "/example", None))
    assert len(fake.calls) == 1
    gate.remember_status("a", "logged_in")
    fake.responses.append({})
    asyncio.run(gate.get(None, "/example", None))
    assert [call[0] for call in fake.calls] == ["b", "a"]


def test_distributed_unknown_auth_failure_pauses_and_fails_over(core):
    from tokenwatch.upstream import AuthRequired

    store, fake, _, gate, _ = core
    with store.begin() as db:
        db.get(Account, "a").available_at = 800
        db.get(Account, "b").available_at = 900
    assert not gate.status_cache
    fake.responses.extend([AuthRequired(), {"ok": True}])
    assert asyncio.run(gate.get(None, "/example", None)) == {"ok": True}
    assert fake.calls == [("a", "/example", None), ("b", "/example", None)]
    with store() as db:
        assert (db.get(Account, "a").status, db.get(Account, "a").pause_reason) == ("paused", "AuthRequired")
    assert gate.status_cache["a"][1] == "logged_out"


def test_daily_matches_lazily_and_collects_every_supported_coin(core, settings):
    store, fake, _, gate, collectors = core
    configure_matching(settings)
    collectors.catalog = CoinCatalog(store, gate, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
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
        created = client.post("/jobs", json=job).json()
        assert created["account_id"] is None
        assert client.put(f"/jobs/{created['id']}", json=job).status_code == 200
        assert client.put("/coins/native:BTC/mapping", json={"pricing_id": "bitcoin"}).status_code == 200
        assert client.get("/coins", params={"status": "pending_support"}).json()[0]["id"] == "native:BTC"
        assert client.put("/coin-catalog/futures-mappings/1000SHIB", json={"symbol": "SHIB"}).status_code == 200
        assert client.put("/coin-catalog/exclusion-rules/tag/stablecoin", json={"reason": "stablecoin"}).status_code == 200
        assert client.get("/coin-catalog/exclusion-rules").json()[0]["kind"] == "tag"


def test_manual_exclusions_are_distinct_from_catalog_rules(settings):
    app = create_app(settings, run_scheduler=False)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        with app.state.store.begin() as db:
            db.add_all([Coin(id="manual", symbol="MANUAL", status="supported", match_status="supported"),
                        Coin(id="rule", symbol="RULE", status="excluded", reason="stablecoin",
                             catalog_status="excluded", catalog_reason="stablecoin")])
        assert client.put("/exclusions/manual", json={"reason": "manual reason"}).json() == {
            "coin_id": "manual", "reason": "manual reason"}
        assert client.get("/exclusions").json() == [{"coin_id": "manual", "reason": "manual reason"}]
        assert client.get("/exclusions/manual").json() == {"coin_id": "manual", "reason": "manual reason"}
        assert client.get("/exclusions/rule").status_code == 404
        assert client.delete("/exclusions/rule").status_code == 404
        coins = {row["id"]: (row["status"], row["reason"]) for row in client.get("/coins").json()}
        assert coins == {"manual": ("excluded", "manual reason"), "rule": ("excluded", "stablecoin")}
        assert client.delete("/exclusions/manual").status_code == 204
        assert client.get("/coins/manual").json()["status"] == "supported"
        assert client.put("/exclusions/rule", json={"reason": "override"}).status_code == 200
        assert client.get("/exclusions/rule").json()["reason"] == "override"
        assert client.get("/coins/rule").json()["reason"] == "override"
        assert client.delete("/exclusions/rule").status_code == 204
        coin = client.get("/coins/rule").json()
        assert (coin["status"], coin["reason"]) == ("excluded", "stablecoin")
        assert client.get("/exclusions").json() == []


def test_catalog_deployments_serialize_without_internal_fields(settings):
    feeds = catalog_feeds()
    feeds[NETWORK_URL]["data"][-1]["networkList"][0]["contractAddress"] = None
    app = create_app(settings, run_scheduler=False)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        catalog = CoinCatalog(app.state.store, None, mock_feeds(feeds, []))
        asyncio.run(catalog.refresh_snapshot())
        with app.state.store() as db:
            rows = db.scalars(select(Coin).where(Coin.symbol.in_(["BTC", "AVAX"]))).all()
            assert any(len(row.deployments) > 1 for row in rows)
            assert all("address_known" in deployment for row in rows for deployment in row.deployments)
            coin_id = next(row.id for row in rows if row.symbol == "BTC")
        for url, params in [("/coins", {}), (f"/coins/{coin_id}", {}),
                            ("/coins", {"status": "needs_manual_mapping"})]:
            response = client.get(url, params=params)
            assert response.status_code == 200
            result = response.json()
            for coin in result if isinstance(result, list) else [result]:
                assert set(coin) == {"id", "symbol", "pricing_id", "status", "reason", "sources",
                                     "deployments", "matched_chains", "match_failures", "last_match_attempt",
                                     "price_check_status", "price_check_ratio", "price_checked_at", "price_check_reason"}
                for deployment in coin["deployments"]:
                    assert set(deployment) == {"feed", "chain", "address", "deposit_enabled", "withdraw_enabled"}


def test_schema_reset_preserves_history_and_jobs(settings):
    store = open_store(settings.database)
    with store.begin() as db:
        db.add_all([Account(id="a", proxy_ref="p"), Coin(id="OLD", symbol="OLD"),
                    Transfer(id="tx", direction="unknown", time=now(), gap=False),
                    Snapshot(interval="7d", truncated=False),
                    Event(dedupe_key="e", kind="alarm", coin_id="OLD", level="normal", push=True, data={})])
        db.flush()
        db.add_all([TransferCoin(transfer_id="tx", coin_id="OLD"), PushLog(event_id=1),
                     Job(id=2, kind="transfers", account_id="a", coin_id="OLD", enabled=True, interval_seconds=60),
                     Job(id=3, kind="daily", account_id="a", enabled=True, interval_seconds=60),
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
        assert not db.get(Job, 2).enabled and db.get(Job, 2).wait_reason == "coin_reassignment_required"
        assert db.get(Job, 3).enabled
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
        assert db.scalar(text("PRAGMA foreign_keys")) == 1
    migrated.kw["bind"].dispose()


@pytest.mark.parametrize("last,expected", [(search(token("found", "C", "c")), "pending_support"),
                                          (search(), "no_match")])
def test_searches_all_deployments(core, settings, last, expected):
    store, fake, clock, gate, _ = core
    configure_matching(settings)
    catalog = CoinCatalog(store, gate, httpx.MockTransport(lambda request: httpx.Response(200, json={})), clock)
    with store.begin() as db:
        pending(db, "multi", "A", "a")
        db.flush()
        coin = db.get(Coin, "multi")
        coin.deployments = [dict(coin.deployments[0], chain=chain, address=address)
                            for chain, address in [("C", "c"), ("A", "a"), ("B", "b")]]
    fake.responses.extend([search(), search({"identifier": {"chain": "B", "address": "b"}}), last])
    for status in ("pending_match", "pending_match", expected):
        asyncio.run(catalog.match_next(SimpleNamespace(account_id="a")))
        with store() as db:
            assert db.get(Coin, "multi").status == status
    assert [call[2]["query"] for call in fake.calls] == ["a", "b", "c"]


def test_failed_deployment_is_retried(core, settings):
    store, fake, clock, gate, _ = core
    configure_matching(settings)
    catalog = CoinCatalog(store, gate, httpx.MockTransport(lambda request: httpx.Response(200, json={})), clock)
    with store.begin() as db:
        pending(db, "multi", "A", "a")
        db.flush()
        coin = db.get(Coin, "multi")
        coin.deployments = coin.deployments + [dict(coin.deployments[0], chain="B", address="b")]
    fake.responses.extend([search(), BadResponse(), search()])
    job = SimpleNamespace(account_id="a")
    asyncio.run(catalog.match_next(job))
    with pytest.raises(BadResponse):
        asyncio.run(catalog.match_next(job))
    with store() as db:
        coin = db.get(Coin, "multi")
        assert (coin.status, coin.match_failures, coin.searched_deployments) == ("pending_match", 1, [["A", "a"]])
    asyncio.run(catalog.match_next(job))
    assert [call[2]["query"] for call in fake.calls] == ["a", "b", "b"]


def test_rematch_only_multi_no_match(core, settings):
    store, _, _, _, _ = core
    with store.begin() as db:
        for name in ("multi", "single", "ambiguous", "mapped"):
            pending(db, name, "A", "a")
        db.flush()
        for name in ("multi", "ambiguous", "mapped"):
            coin = db.get(Coin, name)
            coin.deployments = coin.deployments + [dict(coin.deployments[0], chain="B", address="b")]
        for name in ("multi", "single", "mapped"):
            coin = db.get(Coin, name)
            coin.status = coin.match_status = "no_match"
        coin = db.get(Coin, "ambiguous")
        coin.status = coin.match_status = "ambiguous"
        db.add(CoinMapping(coin_id="mapped", pricing_id="manual"))
    with TestClient(create_app(settings, run_scheduler=False), headers={"Authorization": "Bearer test-token"}) as client:
        assert client.post("/coin-catalog/rematch").json() == {"count": 1}
        assert client.post("/coin-catalog/rematch", json={"statuses": ["no_match"]}).json() == {"count": 0}
    with store() as db:
        coin = db.get(Coin, "multi")
        assert (coin.status, coin.searched_deployments) == ("pending_match", [["A", "a"]])
        assert db.get(Coin, "single").status == "no_match"
        assert db.get(Coin, "ambiguous").status == "ambiguous"
        assert db.get(Coin, "mapped").status == "no_match"


def test_deployment_change_during_search(core, settings):
    store, _, clock, _, _ = core
    configure_matching(settings)
    with store.begin() as db:
        pending(db, "multi", "A", "a")
    class Gate:
        async def get(self, account_id, path, params, decode):
            with store.begin() as db:
                coin = db.get(Coin, "multi")
                coin.deployments = [dict(coin.deployments[0], chain="B", address="b")]
            return decode(search())
    catalog = CoinCatalog(store, Gate(), httpx.MockTransport(lambda request: httpx.Response(200, json={})), clock)
    asyncio.run(catalog.match_next(SimpleNamespace(account_id="a")))
    with store() as db:
        coin = db.get(Coin, "multi")
        assert (coin.status, coin.searched_deployments) == ("pending_match", [])


def test_migrate_v4_preserves_coin(tmp_path):
    path = tmp_path / "v4.db"
    store = open_store(path)
    with store.begin() as db:
        pending(db, "old", "A", "a")
    store.kw["bind"].dispose()
    import sqlite3
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE coins DROP COLUMN searched_deployments")
        connection.execute("PRAGMA user_version=4")
    store = open_store(path)
    with store() as db:
        assert db.get(Coin, "old").searched_deployments == []
        assert db.scalar(text("PRAGMA user_version")) == 9
    store.kw["bind"].dispose()


def test_migrate_v6_preserves_data(tmp_path):
    import sqlite3
    path = tmp_path / "v6.db"
    store = open_store(path)
    with store.begin() as db:
        db.add(Coin(id="old", symbol="OLD", status="supported", pricing_id="old"))
        db.add(CatalogSettings(key="matching", failure_limit=3, refresh_retry_seconds=60))
    store.kw["bind"].dispose()
    with sqlite3.connect(path) as db:
        for column in ("price_check_status", "price_check_ratio", "price_checked_at", "price_check_reason"):
            db.execute(f"ALTER TABLE coins DROP COLUMN {column}")
        for column in ("price_tolerance_pct", "price_max_age_seconds"):
            db.execute(f"ALTER TABLE catalog_settings DROP COLUMN {column}")
        db.execute("PRAGMA user_version=6")
    store = open_store(path)
    with store() as db:
        coin = db.get(Coin, "old")
        assert (coin.symbol, coin.status, coin.pricing_id, coin.price_check_status) == ("OLD", "supported", "old", None)
        assert db.get(CatalogSettings, "matching").failure_limit == 3
        assert db.scalar(text("PRAGMA user_version")) == 9
    store.kw["bind"].dispose()


def test_migrate_v7_clears_results_and_drops_evidence(tmp_path):
    import sqlite3
    path = tmp_path / "v7.db"
    store = open_store(path)
    with store.begin() as db:
        db.add_all([Coin(id="old", symbol="OLD", status="supported", pricing_id="old",
                         price_check_status="mismatch", price_check_ratio=2, price_checked_at=123),
                    Coin(id="manual", symbol="MANUAL", status="supported", pricing_id="manual",
                         price_check_status="ok", price_check_ratio=1, price_checked_at=123)])
        db.flush()
        db.add(CoinMapping(coin_id="manual", pricing_id="manual"))
    store.kw["bind"].dispose()
    with sqlite3.connect(path) as db:
        for name, kind in (("arkham_price", "FLOAT"), ("arkham_price_at", "FLOAT"),
                           ("arkham_chains", "JSON"), ("chain_check_status", "VARCHAR")):
            db.execute(f"ALTER TABLE coins ADD COLUMN {name} {kind}")
        db.execute("PRAGMA user_version=7")
    store = open_store(path)
    with store() as db:
        old, manual = db.get(Coin, "old"), db.get(Coin, "manual")
        assert (old.symbol, old.status, old.pricing_id, old.price_check_status) == ("OLD", "supported", "old", None)
        assert manual.price_check_status == "manual"
        assert manual.price_check_ratio is None and manual.price_checked_at is None
        assert db.scalar(text("PRAGMA user_version")) == 9
        columns = {row[1] for row in db.execute(text("PRAGMA table_info(coins)"))}
        assert not columns.intersection({"arkham_price", "arkham_price_at", "arkham_chains", "chain_check_status"})
    store.kw["bind"].dispose()
