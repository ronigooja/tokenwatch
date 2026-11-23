import asyncio
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from test_core import core, settings, transfer
from tokenwatch.api import create_app
from tokenwatch.coins import Candidate, CoinCatalog, CoinGeckoResolver, binance_candidates, public_json
from tokenwatch.db import Account, CatalogMapping, Coin, CoinMapping, DailyBalance, now
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


def test_gecko_mapping_cache_and_ambiguity(core, settings):
    store, fake, clock, gate, collectors = core
    calls = []
    payload = [{"id": "first", "symbol": "same", "platforms": {"ethereum": "0xABC"}},
               {"id": "second", "symbol": "same", "platforms": {"base": "0xABC"}},
               {"id": "unique", "symbol": "unique", "platforms": {}}]
    def fetch(request):
        calls.append(request)
        return httpx.Response(200, json=payload)
    resolver = CoinGeckoResolver(store, httpx.MockTransport(fetch), clock)
    alpha = Candidate("SAME", [{"feed": "alpha", "chain_id": "1", "contract_address": "0xabc"}])
    async def run():
        assert await resolver.resolve(alpha) == "first"
        assert await resolver.resolve(Candidate("SAME", [{"feed": "spot"}])) is None
        with store() as db:
            assert db.get(CatalogMapping, "SAME").ambiguous
        assert await resolver.resolve(Candidate("UNIQUE", [{"feed": "spot"}])) == "unique"
        other = CoinGeckoResolver(store, httpx.MockTransport(fetch), clock)
        assert await other.resolve(alpha) == "first"
        async def candidates():
            return [alpha]
        await CoinCatalog(store, gate, settings, resolver, candidates).refresh(SimpleNamespace(account_id="a"))
        with store() as db:
            assert not db.get(Coin, "SAME").verified
        clock.value += 7 * 86400
        await other.resolve(alpha)
    asyncio.run(run())
    assert len(calls) == 2
    assert not fake.calls


def test_manual_mapping_overrides_ambiguous_catalog(core, settings):
    store, fake, clock, gate, collectors = core
    with store.begin() as db:
        db.add(CatalogMapping(symbol="ABC", pricing_id=None, ambiguous=True))
        db.add(CoinMapping(coin_id="ABC", pricing_id="manual-id"))
    class NoResolution:
        async def resolve(self, candidate):
            raise AssertionError("Manual overrides must bypass automatic resolution")
    async def candidates():
        return [Candidate("ABC", [{"feed": "spot"}])]
    asyncio.run(CoinCatalog(store, gate, settings, NoResolution(), candidates).refresh(SimpleNamespace(account_id="a")))
    with store() as db:
        coin = db.get(Coin, "ABC")
        assert coin.pricing_id == "manual-id"
        assert not coin.verified
        assert coin.exclusion_reason == "verification_disabled"
    assert not fake.calls


def test_ambiguous_symbol_not_checked(core, settings):
    store, fake, clock, gate, collectors = core
    payload = [{"id": name, "symbol": "same", "platforms": {}} for name in ("one", "two")]
    resolver = CoinGeckoResolver(store, httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), clock)
    async def candidates():
        return [Candidate("SAME", [{"feed": "spot"}])]
    asyncio.run(CoinCatalog(store, gate, settings.model_copy(update={"allow_pricing_id_checks": True}),
                            resolver, candidates).refresh(SimpleNamespace(account_id="a")))
    with store() as db:
        assert db.get(Coin, "SAME").exclusion_reason == "ambiguous_symbol"
        assert not db.get(Coin, "SAME").verified
    assert not fake.calls


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
    with pytest.raises(ValueError):
        asyncio.run(binance_candidates(httpx.MockTransport(lambda request: httpx.Response(200, json={}))))


def test_api_session_reads_share_cache(settings):
    from test_core import FakeUpstream
    fake = FakeUpstream()
    fake.responses.append({"id": "user"})
    app = create_app(settings, fake, run_scheduler=False)
    with TestClient(app) as client:
        client.headers.update({"Authorization": "Bearer test-token"})
        client.put("/accounts/a", json={"proxy_ref": "p"})
        for _ in range(3):
            assert client.get("/accounts/a/session").json() == {"status": "logged_in"}
    assert len(fake.calls) == 1
