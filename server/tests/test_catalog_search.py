from collections import deque
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tokenwatch.api import create_app
from tokenwatch.config import Settings
from tokenwatch.db import Account, Coin, CoinMapping
from tokenwatch.upstream import BadResponse, Blocked, RateLimited, RequestRejected


class FakeUpstream:
    def __init__(self):
        self._config = SimpleNamespace(request_timeout=1)
        self.calls = []
        self.responses = deque()

    async def get_json(self, account_id, path, params):
        self.calls.append((account_id, path, params))
        value = self.responses.popleft()
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def setup(tmp_path):
    settings = Settings(database=tmp_path / "db", bearer_token="token", signing_key="key",
                        request_interval=0.001, request_jitter=0, backoff_initial=0.001)
    fake = FakeUpstream()
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    with TestClient(create_app(settings, fake, catalog_transport=transport, run_scheduler=False)) as client:
        with client.app.state.store.begin() as db:
            db.add_all([Account(id="a", proxy_ref="secret-a"), Account(id="b", proxy_ref="secret-b"),
                        Coin(id="native:BTC", symbol="BTC", status="needs_manual_mapping")])
            db.flush()
            db.add(CoinMapping(coin_id="native:BTC", pricing_id="bitcoin"))
        client.headers["Authorization"] = "Bearer token"
        yield client, fake


def rows(client):
    with client.app.state.store() as db:
        return ([(c.id, c.status, c.pricing_id, c.match_failures, c.last_match_attempt)
                 for c in db.scalars(select(Coin))],
                [(m.coin_id, m.pricing_id) for m in db.scalars(select(CoinMapping))])


def test_socket_gate_changes_and_coalescing(setup):
    client, fake = setup
    app = client.app
    with app.state.store.begin() as db:
        db.get(Account, "b").status = "paused"
    app.state.gate.notify_account("a")
    with client.websocket_connect("/ws/events", headers={"Authorization": "Bearer token"}) as socket:
        fake.responses.append(BadResponse())
        client.get("/coin-catalog/search", params={"query": "BTC"})
        failure = socket.receive_json()
        assert failure["type"] == "account" and failure["data"]["failures"] == 1
        fake.responses.append({})
        client.get("/coin-catalog/search", params={"query": "BTC"})
        success = socket.receive_json()
        assert success["data"]["failures"] == 0
        assert success["data"]["session_status"] == "logged_in"
        fake.responses.append({})
        client.get("/coin-catalog/search", params={"query": "BTC"})
        app.state.gate.remember_status("a", "logged_in")
        app.state.gate.remember_status("a", "logged_out")
        changed = socket.receive_json()
        assert changed["data"]["session_status"] == "logged_out"


def test_socket_pausing_failure(setup):
    client, fake = setup
    with client.app.state.store.begin() as db:
        db.get(Account, "b").status = "paused"
    client.app.state.gate.notify_account("a")
    with client.websocket_connect("/ws/events", headers={"Authorization": "Bearer token"}) as socket:
        fake.responses.append(RequestRejected("fake", status=403))
        assert client.get("/coin-catalog/search", params={"query": "BTC"}).status_code == 502
        frame = socket.receive_json()
        assert (frame["type"], frame["data"]["failures"], frame["data"]["status"],
                frame["data"]["pause_reason"]) == ("account", 1, "paused", "RequestRejected")


def test_search_success_and_empty(setup):
    client, fake = setup
    before = rows(client)
    fake.responses.extend([{"tokens": [{"identifier": {}},
        {"identifier": {"pricingID": "bitcoin"}, "name": 123, "symbol": "BTC",
         "rank": 1, "marketCap": 12, "price": 3.5, "private": "secret",
         "deployments": [{"chain": "bitcoin", "address": "secret"}, None]}] +
        [{"identifier": {"pricingID": f"coin{i}"}} for i in range(30)]}, {}])
    response = client.get("/coin-catalog/search", params={"query": " BTC "})
    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "BTC" and len(data["candidates"]) == 20
    assert data["candidates"][0] == {"pricing_id": "bitcoin", "symbol": "BTC", "rank": 1,
                                      "marketCap": 12, "price": 3.5, "chains": ["bitcoin"]}
    assert client.get("/coin-catalog/search", params={"query": "ETH"}).json() == {"query": "ETH", "candidates": []}
    assert len(fake.calls) == 2 and fake.calls[0][1:] == ("/intelligence/search", {"query": "BTC"})
    assert rows(client) == before


@pytest.mark.parametrize("query", ["", "   ", "x" * 65, "a\nb", "\x7f", "\tBTC"])
def test_invalid_query(setup, query):
    client, fake = setup
    assert client.get("/coin-catalog/search", params={"query": query}).status_code == 422
    assert fake.calls == []


def test_auth_and_no_account(setup):
    client, fake = setup
    assert client.get("/coin-catalog/search", params={"query": "BTC"}, headers={"Authorization": ""}).status_code == 401
    with client.app.state.store.begin() as db:
        for account in db.scalars(select(Account)):
            account.status = "paused"
    assert client.get("/coin-catalog/search", params={"query": "BTC"}).status_code == 503
    assert fake.calls == []


def test_waits_for_gate_and_respects_total_timeout(setup):
    client, fake = setup
    gate = client.app.state.gate
    fake.responses.append({})
    with client.app.state.store.begin() as db:
        for account in db.scalars(select(Account)):
            account.available_at = gate.clock() + 0.05
    assert client.get("/coin-catalog/search", params={"query": "BTC"}).status_code == 200
    assert len(fake.calls) == 1
    gate.upstream._config.request_timeout = 0.02
    with client.app.state.store.begin() as db:
        for account in db.scalars(select(Account)):
            account.available_at = gate.clock() + 1
    assert client.get("/coin-catalog/search", params={"query": "ETH"}).status_code == 503
    assert len(fake.calls) == 1


@pytest.mark.parametrize("error", [Blocked("secret", cause="challenge_403", status=403),
                                   RateLimited("secret", retry_after=5), RequestRejected("secret", status=403)])
def test_pauses_without_retry_or_leak(setup, error):
    client, fake = setup
    before = rows(client)
    fake.responses.append(error)
    response = client.get("/coin-catalog/search", params={"query": "BTC"})
    assert response.status_code == 502 and "secret" not in response.text
    assert len(fake.calls) == 1
    with client.app.state.store() as db:
        account = db.get(Account, fake.calls[0][0])
        assert (account.status, account.pause_reason, account.failures) == ("paused", type(error).__name__, 1)
    assert rows(client) == before
