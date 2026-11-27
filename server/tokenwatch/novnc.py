import asyncio
import logging
from contextlib import suppress
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect

from . import managed_login_view


class _RedactAccess(logging.Filter):
    def filter(self, record):
        return "/login-view/" not in record.getMessage()


def install(app):
    # Uvicorn's access log otherwise contains the bearer-like path credential.
    for name in ("uvicorn.access", "uvicorn.error", "httpx"):
        logging.getLogger(name).addFilter(_RedactAccess())
    managed_login_view.install(app)

    @app.get("/login-view/{ticket}/{asset:path}", include_in_schema=False)
    async def asset(ticket: str, asset: str, request: Request):
        if not app.state.logins.valid(ticket):
            raise HTTPException(404, "Not found")
        if app.state.logins.manager_mode:
            return await managed_login_view.asset(app.state.logins, ticket, asset)
        if any(part in ("..", ".") for part in asset.split("/")) or "\\" in asset:
            raise HTTPException(404, "Not found")
        url = app.state.settings.novnc_url.rstrip("/") + "/" + asset
        client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
        try:
            response = await client.send(client.build_request("GET", url), stream=True)
        except httpx.HTTPError:
            await client.aclose()
            raise HTTPException(502, "noVNC unavailable") from None

        async def stream():
            try:
                async for chunk in response.aiter_raw():
                    if not app.state.logins.valid(ticket):
                        break
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        headers = {key: value for key, value in response.headers.items() if key.lower() in
                   {"content-type", "content-encoding", "content-length"}}
        headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        return StreamingResponse(stream(), status_code=response.status_code, headers=headers)

    @app.websocket("/login-view/{ticket}/websockify")
    async def socket(websocket: WebSocket, ticket: str):
        if app.state.logins.manager_mode or not app.state.logins.valid(ticket):
            await websocket.close(code=1008)
            return
        session = app.state.logins.current
        parts = urlsplit(app.state.settings.novnc_url)
        url = urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc,
                          parts.path.rstrip("/") + "/websockify", "", ""))
        try:
            async with websockets.connect(url, proxy=None) as upstream:
                await websocket.accept(headers=[(b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer")])

                async def send_up():
                    while app.state.logins.valid(ticket):
                        data = await websocket.receive_bytes()
                        await upstream.send(data)

                async def send_down():
                    async for data in upstream:
                        if not app.state.logins.valid(ticket):
                            return
                        if isinstance(data, bytes):
                            await websocket.send_bytes(data)

                tasks = [asyncio.create_task(send_up()), asyncio.create_task(send_down()),
                         asyncio.create_task(session.revoked.wait())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (OSError, websockets.WebSocketException, WebSocketDisconnect):
            pass
        finally:
            with suppress(RuntimeError):
                await websocket.close()
