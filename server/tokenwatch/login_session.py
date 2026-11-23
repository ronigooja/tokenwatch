import asyncio
import secrets
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import urlencode

from fastapi import HTTPException

from .db import Account
from .display import clear_caps_lock


@dataclass
class Login:
    account_id: str
    ticket: str
    previous: tuple[str, str | None]
    page: str = "login"
    state: str = "starting"
    task: asyncio.Task | None = None
    timer: asyncio.Task | None = None
    revoked: asyncio.Event = field(default_factory=asyncio.Event)


class LoginSessions:
    def __init__(self, store, upstream, gate, settings):
        self.store, self.upstream, self.gate, self.settings = store, upstream, gate, settings
        self.current = None
        self.history = {}
        self.lock = asyncio.Lock()

    def valid(self, ticket):
        session = self.current
        return bool(session and session.ticket and secrets.compare_digest(session.ticket, ticket))

    def path(self, session):
        query = {"autoconnect": "true", "resize": "scale", "path": f"login-view/{session.ticket}/websockify"}
        if self.settings.vnc_password:
            query["password"] = self.settings.vnc_password.get_secret_value()
        return f"/login-view/{session.ticket}/vnc.html?{urlencode(query, safe='/')}"

    def view(self, account_id):
        session = self.current if self.current and self.current.account_id == account_id else None
        return {"state": session.state, "path": self.path(session)} if session else {
            "state": self.history.get(account_id, "none"), "path": None}

    async def start(self, account_id, *, page="login"):
        async with self.lock:
            with self.store.begin() as db:
                account = db.get(Account, account_id)
                if not account:
                    raise HTTPException(404, "Resource not found")
                if self.current:
                    raise HTTPException(409, "Login session already exists")
                if not self.settings.novnc_url or not callable(getattr(self.upstream, "manual_login", None)):
                    raise HTTPException(503, "Login unavailable")
                session = Login(account_id, secrets.token_urlsafe(32), (account.status, account.pause_reason), page)
                account.status, account.pause_reason = "paused", "login"
            self.current = session
            self.history.pop(account_id, None)
            await asyncio.to_thread(clear_caps_lock)
            session.task = asyncio.create_task(self._run(session))
            session.timer = asyncio.create_task(self._timeout(session))
            return {"state": "starting", "path": self.path(session)}

    async def _run(self, session):
        def ready():
            session.state = "open"

        try:
            await self.upstream.manual_login(session.account_id, ready=ready, page=session.page)
        except asyncio.CancelledError:
            raise
        except Exception:
            if session.state == "starting":
                session.state = "failed"
                with self.store.begin() as db:
                    account = db.get(Account, session.account_id)
                    if account and account.pause_reason == "login":
                        account.status, account.pause_reason = session.previous
            else:
                session.state = "closed"
        else:
            session.state = "closed"

    async def _timeout(self, session):
        await asyncio.sleep(self.settings.login_timeout)
        async with self.lock:
            if self.current is session:
                await self._close(session, activate=session.state != "failed")

    async def _close(self, session, *, activate):
        session.ticket = ""
        session.revoked.set()
        if session.timer is not asyncio.current_task():
            session.timer.cancel()
        session.task.cancel()
        with suppress(asyncio.CancelledError):
            await session.task
        if activate:
            with self.store.begin() as db:
                account = db.get(Account, session.account_id)
                if account and account.pause_reason == "login":
                    account.status, account.pause_reason = "active", None
        self.history[session.account_id] = "closed" if activate else "failed"
        self.current = None

    async def finish(self, account_id):
        async with self.lock:
            session = self.current
            if not session or session.account_id != account_id:
                raise HTTPException(404, "Login session not found")
            await self._close(session, activate=True)
            self.gate.status_cache.pop(account_id, None)
            return {"status": await self.gate.account_status(account_id)}

    async def shutdown(self):
        async with self.lock:
            if self.current:
                await self._close(self.current, activate=self.current.state != "failed")
