import asyncio
import base64
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tokenwatch.api import create_app
from tokenwatch.browser.client import BrowserUpstreamClient, _BrowserFailure, sign_payload
from tokenwatch.browser.config import AccountConfig, BrowserConfig
from tokenwatch.browser.manager import Manager
from tokenwatch.config import Settings
from tokenwatch.db import Account
from tokenwatch.upstream import Blocked, RateLimited


PROXY = "socks5h://example:example@proxy.example:1080"
PNG = b"\x89PNG\r\n\x1a\n"
SOCKET = Path("/test/manager.sock")


def config(tmp_path, **options):
    return BrowserConfig(None, None, "test-key", (AccountConfig("a", PROXY, 1, email="a@example.test",
                                                               password="saved"),), 45, 0.1,
                         manager_socket=SOCKET, manager_profiles={"a": "profile1"},
                         managed_ids=frozenset({"a"}), accounts_file=tmp_path / "accounts.local.toml", **options)


class FakeManager:
    def __init__(self):
        self.calls = []
        self.pages = {}
        self.title = "Arkham"
        self.status = 200
        self.retry_after = None
        self.fetch_error = None
        self.delay_fetch = None
        self.fail_open = False

    async def __call__(self, request):
        assert request.method == "POST" and request.url.path == "/v1/project"
        body = json.loads(request.content)
        op, args = body["op"], body["args"]
        self.calls.append((op, args))
        assert args["profile_id"] == "profile1"
        page = args["page_name"]
        result = {}
        if op == "open":
            if self.fail_open:
                return httpx.Response(503, json={"error": {"code": "unavailable"}})
            self.pages.setdefault(page, args["url"])
            result = {"url": self.pages[page], "generation": 1}
        if op == "info":
            if page not in self.pages:
                return httpx.Response(404, json={"error": {"code": "not_found"}})
            result = {"url": self.pages[page], "title": self.title, "generation": 1}
        if op == "navigate":
            self.pages[page] = args["url"]
        if op == "fetch":
            if self.delay_fetch is not None:
                await asyncio.sleep(self.delay_fetch)
            if self.fetch_error:
                return httpx.Response(503, json={"error": {"code": self.fetch_error, "detail": "private-value"}})
            result = {"status": self.status, "body": '{"user":"mock"}', "retry_after": self.retry_after,
                      "cf_mitigated": None}
        if op == "screenshot":
            result = {"png": base64.b64encode(PNG).decode()}
        if op == "close":
            self.pages.pop(page, None)
        return httpx.Response(200, json={"result": result})

    def client(self, cfg):
        return BrowserUpstreamClient(cfg, manager=Manager(SOCKET, transport=httpx.MockTransport(self)), clock=lambda: 1234)


def test_config_requires_unique_complete_operator_mapping(tmp_path):
    path = tmp_path / "browser.local.toml"
    path.write_text(f'''request_timeout = 45
close_timeout = 2
manager_socket = "project.sock"
[manager_profiles]
a = "profile1"
[[accounts]]
id = "a"
proxy_url = "{PROXY}"
fingerprint_seed = 1
''')
    cfg = BrowserConfig.from_toml(path, "key")
    assert cfg.manager_socket == tmp_path / "project.sock"
    assert cfg.binary_path is None
    assert cfg.manager_profiles == {"a": "profile1"}
    path.write_text(path.read_text().replace('a = "profile1"', 'b = "profile1"'))
    with pytest.raises(ValueError, match="manager_profiles"):
        BrowserConfig.from_toml(path, "key")


def test_signed_single_fetch_uses_existing_classifier(tmp_path):
    fake = FakeManager()

    async def run():
        async with fake.client(config(tmp_path)) as client:
            fake.status, fake.retry_after = 429, "17"
            with pytest.raises(RateLimited) as error:
                await client.get_json("a", "/transfers", {"query": "a b"})
            assert error.value.retry_after == 17
    asyncio.run(run())
    assert [op for op, _ in fake.calls] == ["open", "info", "fetch", "close"]
    assert fake.calls[2][1] == {"profile_id": "profile1", "page_name": "tokenwatch-automation",
                                "url": "https://api.arkm.com/transfers?query=a+b", "origin": "https://arkm.com",
                                "headers": {"X-Timestamp": "1234", "X-Payload": sign_payload("/transfers", 1234, "test-key")}}


def test_unexpected_verification_does_not_fetch_or_click(tmp_path):
    fake = FakeManager()
    fake.title = "Just a moment…"

    async def run():
        async with fake.client(config(tmp_path)) as client:
            with pytest.raises(Blocked) as error:
                await client.get_json("a", "/user")
            assert error.value.cause == "challenge_timeout"
    asyncio.run(run())
    assert [op for op, _ in fake.calls] == ["open", "info", "close"]


def test_restored_blank_page_is_prepared_before_one_fetch(tmp_path):
    fake = FakeManager()
    fake.pages["tokenwatch-automation"] = "about:blank"

    async def run():
        async with fake.client(config(tmp_path)) as client:
            assert await client.get_json("a", "/user") == {"user": "mock"}
    asyncio.run(run())
    assert fake.calls[0][1]["restore_url"] == "https://arkm.com/"
    assert [op for op, _ in fake.calls] == ["open", "info", "navigate", "info", "fetch", "close"]


def test_manager_error_drops_page_without_retrying_or_leaking_error_detail(tmp_path):
    fake = FakeManager()
    fake.fetch_error = "unavailable"

    async def run():
        async with fake.client(config(tmp_path)) as client:
            with pytest.raises(_BrowserFailure) as error:
                await client.get_json("a", "/user")
            assert error.value.detail == "Browser Manager: unavailable"
            assert not fake.pages
    asyncio.run(run())
    assert [op for op, _ in fake.calls] == ["open", "info", "fetch", "close"]


def test_cancelled_fetch_is_not_retried(tmp_path):
    fake = FakeManager()
    fake.delay_fetch = 20

    async def run():
        async with fake.client(config(tmp_path)) as client:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    await client.get_json("a", "/user")
            assert not fake.pages
    asyncio.run(run())
    assert [op for op, _ in fake.calls] == ["open", "info", "fetch", "close"]


def test_ticket_scoped_login_finish_and_account_mutations(tmp_path):
    fake = FakeManager()
    settings = Settings(database=tmp_path / "test.db", bearer_token="token", signing_key="key",
                        request_interval=0.001, request_jitter=0)
    app = create_app(settings, fake.client(config(tmp_path)), run_scheduler=False)
    auth = {"Authorization": "Bearer token"}
    with TestClient(app) as client:
        with app.state.store.begin() as db:
            db.add(Account(id="a", proxy_ref="p"))
        assert client.post("/accounts/a/login").status_code == 401
        start = client.post("/accounts/a/login", headers=auth)
        path = start.json()["path"]
        assert start.status_code == 201 and path.endswith("/browser.html")
        assert client.get(path).status_code == 200
        base = path.rsplit("/", 1)[0]
        image = client.get(base + "/screen.png")
        assert image.status_code == 200 and image.content == PNG
        assert image.headers["cache-control"] == "no-store"
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(base + "/websockify"):
                pass
        assert client.get(base + "/vnc.html").status_code == 404
        assert client.post("/login-view/wrong/input", json={"kind": "click", "x": 3, "y": 4}).status_code == 404
        assert client.post(base + "/input", json={"kind": "click", "x": True, "y": 4}).status_code == 422
        assert client.post(base + "/input", json={"kind": "click", "x": 3, "y": 4}).status_code == 204
        assert client.post("/accounts/a/login/finish", headers=auth).json() == {"status": "logged_in"}
        assert client.get(path).status_code == 404
        assert client.post(base + "/input", json={"kind": "key", "key": "Enter"}).status_code == 404
        assert client.put("/accounts/a", json={"proxy_url": PROXY}, headers=auth).status_code == 409
        assert client.post("/accounts", json={"email": "b@example.test", "password": "p", "proxy_url": PROXY},
                           headers=auth).status_code == 409
        assert client.delete("/accounts/a", headers=auth).status_code == 409
        password = client.put("/accounts/a", json={"password": "new"}, headers=auth)
        assert password.status_code == 200 and password.json()["password"] == "new"
    login_page = fake.calls[0][1]["page_name"]
    assert login_page.startswith("tokenwatch-login-") and fake.calls[0][1]["persistent"] is False
    assert not any(op == "fill" for op, _ in fake.calls)
    assert [args["page_name"] for op, args in fake.calls if op == "fetch"] == ["tokenwatch-automation"]
    assert any(op == "close" and args["page_name"] == login_page for op, args in fake.calls)


@pytest.mark.parametrize("end", ["cancel", "timeout", "launch_failure"])
def test_login_termination_closes_page_and_revokes_ticket_without_fetch(tmp_path, end):
    fake = FakeManager()
    fake.fail_open = end == "launch_failure"
    settings = Settings(database=tmp_path / "test.db", bearer_token="token", signing_key="key", login_timeout=0.2)
    app = create_app(settings, fake.client(config(tmp_path)), run_scheduler=False)
    with TestClient(app, headers={"Authorization": "Bearer token"}) as client:
        with app.state.store.begin() as db:
            db.add(Account(id="a", proxy_ref="p", status="paused", pause_reason="Blocked"))
        path = client.post("/accounts/a/login").json()["path"]
        if end == "cancel":
            assert client.post("/accounts/a/login/cancel").json()["state"] == "closed"
        else:
            deadline = time.monotonic() + 2
            while client.get(path).status_code != 404:
                assert time.monotonic() < deadline, "Ticket not revoked by login timeout"
                time.sleep(0.015)
        assert client.get(path).status_code == 404
        with app.state.store() as db:
            account = db.get(Account, "a")
            assert (account.status, account.pause_reason) == (("active", None) if end == "timeout" else ("paused", "Blocked"))
    assert not fake.pages
    assert not any(op == "fetch" for op, _ in fake.calls)


def test_real_http_envelope_over_unix_socket(tmp_path):
    path = tmp_path / "project.sock"

    async def run():
        seen = []

        async def serve(reader, writer):
            try:
                head = (await reader.readuntil(b"\r\n\r\n")).decode()
                assert head.startswith("POST /v1/project HTTP/1.1\r\n")
                length = next(int(line.split(":", 1)[1]) for line in head.splitlines()
                              if line.lower().startswith("content-length:"))
                seen.append(json.loads(await reader.readexactly(length)))
                data = b'{"result":{"url":"about:blank","title":"","generation":1}}'
                writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                             + str(len(data)).encode() + b"\r\n\r\n" + data)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_unix_server(serve, path)
        manager = Manager(path)
        try:
            async with server, asyncio.timeout(2):
                assert (await manager.call("info", "p1", "n"))["generation"] == 1
        finally:
            await manager.close()
        assert seen == [{"op": "info", "args": {"profile_id": "p1", "page_name": "n"}}]
    asyncio.run(run())
