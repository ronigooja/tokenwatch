import os

import pytest
from fastapi.testclient import TestClient

from tokenwatch.api import create_app
from tokenwatch.browser.client import BrowserUpstreamClient
from tokenwatch.browser.config import AccountConfig, BrowserConfig
from tokenwatch.config import Settings
from tokenwatch.db import Account, Job

PROXY = "socks5h://secret-user:secret-password@proxy.example:1080"
PASSWORD = 'secret-"\\é#'
A = "a+tag@example.com"
B = "b@example.com"


@pytest.fixture
def accounts(tmp_path):
    source = tmp_path / "browser.toml"
    source.write_text('binary_path = "chrome"\nprofiles_root = "profiles"\n')
    pool = BrowserUpstreamClient(BrowserConfig.from_toml(source, "key"))
    async def detector(account):
        return "America/New_York"
    app = create_app(Settings(database=tmp_path / "db", bearer_token="token", signing_key="key"), pool,
                     run_scheduler=False, timezone_detector=detector)
    with TestClient(app, headers={"Authorization": "Bearer token"}) as client:
        yield client, pool, source, app


def body(email=A, **changes):
    return {"email": email, "password": PASSWORD, "proxy_url": PROXY, **changes}


def test_create_reload_and_delete(accounts):
    client, pool, source, _ = accounts
    first = client.post("/accounts", json=body(" A+Tag@Example.COM "))
    assert first.status_code == 201
    assert first.json()["id"] == A
    assert first.json()["proxy_ref"] == "proxy-" + A
    assert first.json()["managed"] and first.json()["persona"] == "windows"
    assert first.json()["password"] == PASSWORD and first.json()["proxy_url"] == PROXY
    assert client.post("/accounts", json=body(A.upper())).status_code == 409
    assert client.post("/accounts", json=body(B)).status_code == 201
    config = BrowserConfig.from_toml(source, "key")
    assert len({a.fingerprint_seed for a in config.accounts}) == 2
    assert config.accounts[0].password == PASSWORD
    assert os.stat(config.accounts_file).st_mode & 0o777 == 0o600
    assert client.get("/accounts").json()[0]["email"] == A
    assert client.delete(f"/accounts/{A}").status_code == 204
    assert pool.account_config(A) == (None, False)
    assert [a.id for a in BrowserConfig.from_toml(source, "key").accounts] == [B]


@pytest.mark.parametrize("changes", [dict(email="bad"), dict(email="a..b@example.com"),
    dict(email=".bad@example.com"), dict(email="../escape@example.com"), dict(email="a\\b@example.com"),
    dict(email="a" * 129 + "@x.com"), dict(password=""), dict(password="x" * 257), dict(proxy_url="http://secret")])
def test_invalid(accounts, changes):
    client, _, _, _ = accounts
    result = client.post("/accounts", json=body(**changes))
    assert result.status_code == 422
    assert "secret" not in result.text


@pytest.mark.parametrize("id", ["../escape", ".hidden", "x..y", "x/y", "x\\y"])
def test_profile_traversal(id):
    with pytest.raises(ValueError):
        AccountConfig(id, PROXY, 1)


def test_write_failures(accounts, monkeypatch, caplog):
    client, pool, source, _ = accounts
    assert client.post("/accounts", json=body()).status_code == 201
    path = source.parent / "accounts.local.toml"
    original = path.read_bytes()
    def fail(*args):
        raise OSError("secret-password and path")
    monkeypatch.setattr("tokenwatch.browser.client.write_accounts", fail)
    for response in (client.post("/accounts", json=body(B)), client.delete(f"/accounts/{A}"),
                     client.put(f"/accounts/{A}", json={"password": "new-secret", "status": "paused"})):
        assert response.status_code == 503
        assert "secret" not in response.text and str(source.parent) not in response.text
    assert path.read_bytes() == original
    assert pool.account_config(A)[0].password == PASSWORD
    assert client.get(f"/accounts/{A}").json()["status"] == "active"
    assert client.get(f"/accounts/{B}").status_code == 404
    assert "secret" not in caplog.text
    assert "secret" not in client.get("/events").text


def test_detection_failure_and_edit(accounts):
    client, pool, source, app = accounts
    assert client.post("/accounts", json=body()).status_code == 201
    original = (source.parent / "accounts.local.toml").read_bytes()
    seed = pool.account_config(A)[0].fingerprint_seed
    async def fail(account):
        raise RuntimeError("secret")
    app.state.timezone_detector = fail
    assert client.post("/accounts", json=body(B)).status_code == 502
    result = client.put(f"/accounts/{A}", json={"proxy_url": PROXY.replace("proxy.example", "other.example"), "status": "paused"})
    assert result.status_code == 502 and "secret" not in result.text
    assert (source.parent / "accounts.local.toml").read_bytes() == original
    assert client.get(f"/accounts/{A}").json()["status"] == "active"
    async def detected(account):
        assert "other.example" in account.proxy_url
        return "Asia/Tokyo"
    app.state.timezone_detector = detected
    result = client.put(f"/accounts/{A}", json={"proxy_url": PROXY.replace("proxy.example", "other.example"), "password": "new", "status": "paused"})
    assert result.status_code == 200 and result.json()["timezone"] == "Asia/Tokyo"
    assert pool.account_config(A)[0].fingerprint_seed == seed
    assert pool.account_config(A)[0].persona == "windows"
    assert client.put(f"/accounts/{A}", json={"status": "active"}).json()["pause_reason"] is None


def test_delete_constraints(accounts):
    client, _, _, app = accounts
    assert client.post("/accounts", json=body()).status_code == 201
    with app.state.store.begin() as db:
        db.add(Account(id="static", proxy_ref="static", status="active"))
        db.add(Job(kind="daily", account_id=A, options={}, interval_seconds=60, enabled=False))
    assert client.delete("/accounts/static").status_code == 409
    assert client.delete(f"/accounts/{A}").status_code == 409
    assert client.put("/accounts/static", json={"password": "secret"}).status_code == 409
    assert client.put("/accounts/static", json={"status": "paused"}).status_code == 200
    assert client.put("/accounts/missing", json={}).status_code == 404
