import asyncio
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tokenwatch.api import create_app
from tokenwatch.browser.client import BrowserUpstreamClient
from tokenwatch.browser.config import AccountConfig, BrowserConfig
from tokenwatch.config import Settings
from tokenwatch.db import Account, Job

PROXY = "socks5h://secret-user:secret-password@proxy.example:1080"


@pytest.fixture
def accounts(tmp_path):
    source = tmp_path / "browser.toml"
    source.write_text('binary_path = "chrome"\nprofiles_root = "profiles"\n')
    config = BrowserConfig.from_toml(source, "key")
    pool = BrowserUpstreamClient(config)
    app = create_app(Settings(database=tmp_path / "db", bearer_token="token", signing_key="key"), pool, run_scheduler=False)
    with TestClient(app, headers={"Authorization": "Bearer token"}) as client:
        yield client, pool, source, app


def body(id="a", **changes):
    return {"id": id, "proxy_url": PROXY, "timezone": "America/New_York", **changes}


def test_create_reload_and_delete(accounts):
    client, pool, source, app = accounts
    first = client.post("/accounts", json=body())
    assert first.status_code == 201
    assert first.json()["managed"] and first.json()["persona"] == "windows"
    assert "secret" not in first.text
    assert client.post("/accounts", json=body("A")).status_code == 409
    assert client.post("/accounts", json=body("b")).status_code == 201
    config = BrowserConfig.from_toml(source, "key")
    assert len({a.fingerprint_seed for a in config.accounts}) == 2
    assert os.stat(config.accounts_file).st_mode & 0o777 == 0o600
    assert "secret" not in client.get("/accounts").text
    assert client.delete("/accounts/a").status_code == 204
    assert pool.account_config("a") == (None, False)
    assert [a.id for a in BrowserConfig.from_toml(source, "key").accounts] == ["b"]


@pytest.mark.parametrize("changes", [dict(id="bad.id"), dict(proxy_url="http://secret"),
                                      dict(timezone="No/Such_Zone"), dict(persona="secret")])
def test_invalid(accounts, changes):
    client, _, _, _ = accounts
    result = client.post("/accounts", json=body(**changes))
    assert result.status_code == 422
    assert "secret" not in result.text


def test_write_failures(accounts, monkeypatch):
    client, pool, source, app = accounts
    assert client.post("/accounts", json=body()).status_code == 201
    path = source.parent / "accounts.local.toml"
    original = path.read_bytes()
    def fail(*args):
        raise OSError("secret-password and path")
    monkeypatch.setattr("tokenwatch.browser.client.write_accounts", fail)
    for response in (client.post("/accounts", json=body("b")), client.delete("/accounts/a")):
        assert response.status_code == 503
        assert "secret" not in response.text and str(source.parent) not in response.text
    assert path.read_bytes() == original
    assert pool.account_config("a")[1] and pool.account_config("b") == (None, False)
    assert client.get("/accounts/a").status_code == 200
    assert client.get("/accounts/b").status_code == 404


def test_delete_constraints(accounts):
    client, pool, _, app = accounts
    assert client.post("/accounts", json=body()).status_code == 201
    with app.state.store.begin() as db:
        db.add(Account(id="static", proxy_ref="static", status="active"))
        db.add(Job(kind="daily", account_id="a", options={}, interval_seconds=60, enabled=False))
    assert client.delete("/accounts/static").status_code == 409
    assert client.delete("/accounts/a").status_code == 409
    assert client.put("/accounts/missing", json={"proxy_ref": "x"}).status_code == 404
