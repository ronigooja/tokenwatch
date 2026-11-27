import asyncio
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import pytest
import websockets
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tokenwatch.api import create_app
from tokenwatch.config import Settings
from tokenwatch.db import Account
from tokenwatch.upstream import AuthRequired


class Upstream:
    def __init__(self):
        self.mode = "open"
        self.calls = []
        self.end = None
        self.response = {"id": "user"}

    async def manual_login(self, account_id, *, ready, page="login"):
        self.page = page
        if self.mode == "fail":
            raise RuntimeError("launch failed")
        self.end = asyncio.Event()
        ready()
        await self.end.wait()

    async def get_json(self, account_id, path, params):
        self.calls.append((account_id, path))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (self.path + "|" + str(self.headers.get("Authorization")) + "|" +
                str(self.headers.get("Cookie"))).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def setup(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(database=tmp_path / "test.db", bearer_token="token", signing_key="key",
                        novnc_url=f"http://127.0.0.1:{server.server_port}", vnc_password="secret password",
                        request_interval=0.001, request_jitter=0, login_timeout=0.3)
    fake = Upstream()
    app = create_app(settings, fake, catalog_transport=httpx.MockTransport(lambda request: httpx.Response(500)), run_scheduler=False)
    with TestClient(app, headers={"Authorization": "Bearer token"}) as client:
        with app.state.store.begin() as db:
            db.add_all([Account(id="a", proxy_ref="p"), Account(id="b", proxy_ref="q")])
        yield client, app, fake
    server.shutdown()
    server.server_close()
    thread.join()


def wait_state(client, state):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = client.get("/accounts/a/login").json()
        if result["state"] == state:
            return result
        time.sleep(0.005)
    pytest.fail(f"state did not become {state}: {result}")


def test_maintenance_rejected_in_local_mode(setup):
    client, app, fake = setup
    response = client.post("/accounts/a/login", json={"page": "maintenance"})
    assert response.status_code == 503
    assert app.state.logins.current is None
    with app.state.store() as db:
        assert db.get(Account, "a").pause_reason != "login"


def test_start_finish_ticket_and_single_probe(setup, caplog):
    client, app, fake = setup
    caplog.set_level(logging.DEBUG)
    assert client.post("/accounts/missing/login").status_code == 404
    result = client.post("/accounts/a/login")
    assert result.status_code == 201 and result.json()["state"] == "starting"
    path = result.json()["path"]
    assert "password=secret+password" in path
    wait_state(client, "open")
    assert fake.page == "login"
    assert client.post("/accounts/b/login").status_code == 409
    with app.state.store() as db:
        assert (db.get(Account, "a").status, db.get(Account, "a").pause_reason) == ("paused", "login")
    asset = urlsplit(path).path
    response = client.get(asset, headers={"Cookie": "session=secret"})
    assert response.text == "/vnc.html|None|None"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert client.get("/login-view/wrong/vnc.html").status_code == 404
    assert client.post("/accounts/a/login/finish").json() == {"status": "logged_in"}
    assert fake.calls == [("a", "/user")]
    rows = {row["id"]: row for row in client.get("/accounts").json()}
    assert rows["a"]["session_status"] == "logged_in"
    assert rows["a"]["session_checked_at"] == app.state.gate.status_cache["a"][0]
    assert fake.calls == [("a", "/user")]
    assert client.get(asset).status_code == 404
    assert client.post("/accounts/a/login/finish").status_code == 404
    assert "secret password" not in caplog.text
    assert asset.split("/")[2] not in caplog.text


def test_register_page(setup):
    client, _, fake = setup
    assert client.post("/accounts/a/login", json={"page": "invalid"}).status_code == 422
    assert client.post("/accounts/a/login", json={"page": "register"}).status_code == 201
    wait_state(client, "open")
    assert fake.page == "register"


@pytest.mark.parametrize("previous", [("active", None), ("paused", "Blocked")])
def test_cancel_restores_previous_without_probe(setup, previous):
    client, app, fake = setup
    app.state.settings.login_timeout = 10
    with app.state.store.begin() as db:
        account = db.get(Account, "a")
        account.status, account.pause_reason = previous
        account.failures, account.available_at = 3, 123
    app.state.gate.status_cache["a"] = (123, "logged_out")
    assert client.post("/accounts/a/login/cancel").status_code == 404
    path = client.post("/accounts/a/login").json()["path"]
    wait_state(client, "open")
    session = app.state.logins.current
    assert client.post("/accounts/b/login/cancel").status_code == 404
    assert client.post("/accounts/a/login/cancel").json() == {"state": "closed", "path": None}
    assert session.revoked.is_set() and session.task.cancelled() and session.timer.cancelling()
    assert client.get("/accounts/a/login").json() == {"state": "closed", "path": None}
    assert client.get(urlsplit(path).path).status_code == 404
    assert client.post("/accounts/a/login/cancel").status_code == 404
    assert client.post("/accounts/a/login/finish").status_code == 404
    assert not fake.calls
    assert app.state.gate.status_cache["a"] == (123, "logged_out")
    with app.state.store() as db:
        account = db.get(Account, "a")
        assert (account.status, account.pause_reason) == previous
        assert (account.failures, account.available_at) == (3, 123)


def test_cancel_preserves_changed_pause_reason(setup):
    client, app, fake = setup
    app.state.settings.login_timeout = 10
    client.post("/accounts/a/login")
    wait_state(client, "open")
    with app.state.store.begin() as db:
        account = db.get(Account, "a")
        account.status, account.pause_reason = "paused", "Blocked"
    assert client.post("/accounts/a/login/cancel").status_code == 200
    with app.state.store() as db:
        account = db.get(Account, "a")
        assert (account.status, account.pause_reason) == ("paused", "Blocked")
    assert not fake.calls


def test_failed_timeout_closed_and_auth_pause(setup):
    client, app, fake = setup
    with app.state.store.begin() as db:
        account = db.get(Account, "a")
        account.status, account.pause_reason = "paused", "manual"
    fake.mode = "fail"
    assert client.post("/accounts/a/login").status_code == 201
    wait_state(client, "failed")
    with app.state.store() as db:
        assert (db.get(Account, "a").status, db.get(Account, "a").pause_reason) == ("paused", "manual")
    time.sleep(0.35)
    fake.mode = "open"
    path = client.post("/accounts/a/login").json()["path"]
    wait_state(client, "open")
    time.sleep(0.35)
    assert client.get(urlsplit(path).path).status_code == 404
    with app.state.store() as db:
        assert db.get(Account, "a").status == "active"
    assert not fake.calls
    client.post("/accounts/a/login")
    wait_state(client, "open")
    client.portal.call(fake.end.set)
    wait_state(client, "closed")
    fake.response = AuthRequired()
    assert client.post("/accounts/a/login/finish").json()["status"] == "logged_out"
    with app.state.store() as db:
        assert db.get(Account, "a").pause_reason == "AuthRequired"


def test_websocket_proxy(setup):
    client, app, _ = setup
    loop = asyncio.new_event_loop()
    async def echo(socket):
        async for message in socket:
            await socket.send(message)
    async def start():
        return await websockets.serve(echo, "127.0.0.1", 0)
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    server = asyncio.run_coroutine_threadsafe(start(), loop).result()
    app.state.settings.novnc_url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        path = client.post("/accounts/a/login").json()["path"]
        ticket = urlsplit(path).path.split("/")[2]
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/login-view/wrong/websockify"):
                pass
        assert error.value.code == 1008
        with client.websocket_connect(f"/login-view/{ticket}/websockify") as socket:
            socket.send_bytes(b"hello")
            assert socket.receive_bytes() == b"hello"
            client.post("/accounts/a/login/finish")
            assert socket.receive()["type"] == "websocket.close"
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/login-view/{ticket}/websockify"):
                pass
    finally:
        async def stop():
            server.close()
            await server.wait_closed()
        asyncio.run_coroutine_threadsafe(stop(), loop).result()
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()
