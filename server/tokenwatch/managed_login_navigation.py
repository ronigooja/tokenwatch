import asyncio
import json
import unicodedata
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .browser.manager import ManagerError
from .managed_login_view import HEADERS, _check


def valid_url(url):
    if not isinstance(url, str) or not url or len(url) > 8192:
        return False
    if any(c == "\\" or c.isspace() or unicodedata.category(c) in ("Cc", "Cs") for c in url):
        return False
    try:
        parts = urlsplit(url)
        return (parts.scheme.lower() in ("http", "https") and bool(parts.hostname)
                and parts.username is None and parts.password is None and parts.port != 0)
    except ValueError:
        return False


def capture(logins, ticket):
    session = _check(logins, ticket)
    if session.state != "open":
        raise HTTPException(409, "Browser starting", headers=HEADERS)
    page = logins.upstream._login_pages.get(session.account_id)
    if page is None:
        raise HTTPException(502, "Browser unavailable", headers=HEADERS)
    return session, page


def check_current(logins, ticket, session, page):
    if (_check(logins, ticket) is not session or session.state != "open"
            or logins.upstream._login_pages.get(session.account_id) is not page):
        raise HTTPException(404, "Not found", headers=HEADERS)


async def operate(logins, ticket, session, page, url=None):
    try:
        if url is not None:
            autofill = logins.upstream._login_autofill.get(session.account_id)
            if autofill is not None:
                autofill.cancel()
                await asyncio.gather(autofill, return_exceptions=True)
            check_current(logins, ticket, session, page)
        async with asyncio.timeout(logins.upstream._config.close_timeout):
            if url is not None:
                await page.call("navigate", url=url)
                check_current(logins, ticket, session, page)
            result = await page.info()
        check_current(logins, ticket, session, page)
        return JSONResponse({"url": result["url"]}, headers=HEADERS)
    except (ManagerError, TimeoutError):
        check_current(logins, ticket, session, page)
        raise HTTPException(502, "Browser unavailable", headers=HEADERS) from None


def install(app):
    @app.get("/login-view/{ticket}/location", include_in_schema=False)
    async def location(ticket: str):
        logins = app.state.logins
        session, page = capture(logins, ticket)
        return await operate(logins, ticket, session, page)

    @app.post("/login-view/{ticket}/navigate", include_in_schema=False)
    async def navigate(ticket: str, request: Request):
        logins = app.state.logins
        session, page = capture(logins, ticket)
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            raise HTTPException(415, "Expected JSON", headers=HEADERS)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 16384:
                raise HTTPException(413, "Input too large", headers=HEADERS)
            body.extend(chunk)
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            raise HTTPException(422, "Invalid URL", headers=HEADERS) from None
        if not isinstance(data, dict) or set(data) != {"url"} or not valid_url(data["url"]):
            raise HTTPException(422, "Invalid URL", headers=HEADERS)
        check_current(logins, ticket, session, page)
        return await operate(logins, ticket, session, page, data["url"])
