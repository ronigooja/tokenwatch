import base64
import binascii
import random
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..upstream import AuthRequired, Blocked
from .transport import Response, SITE_URL


class ManagerError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class Manager:
    def __init__(self, socket: Path, *, transport=None):
        self.client = httpx.AsyncClient(
            base_url="http://localhost",
            transport=transport if transport is not None else httpx.AsyncHTTPTransport(uds=str(socket), retries=0),
            trust_env=False, follow_redirects=False, timeout=None,
        )

    async def call(self, op, profile_id, page_name, **args):
        try:
            response = await self.client.post(
                "/v1/project", json={"op": op, "args": {"profile_id": profile_id, "page_name": page_name, **args}},
            )
        except httpx.HTTPError:
            raise ManagerError("unavailable") from None
        try:
            payload = response.json()
            if response.is_success and isinstance(payload, dict) and "result" in payload and "error" not in payload:
                return payload["result"]
            code = payload["error"]["code"]
            if code in ("forbidden", "not_found", "invalid", "timeout", "unavailable", "internal"):
                raise ManagerError(code)
        except (ValueError, TypeError, KeyError, RecursionError):
            pass
        raise ManagerError("invalid_response")

    async def close(self):
        await self.client.aclose()


class ManagerPage:
    def __init__(self, manager: Manager, profile_id: str, name: str, rng=None):
        self.manager, self.profile_id, self.name = manager, profile_id, name
        self._context = ""
        self._rng = rng if rng is not None else random.Random()
        self._viewport = None

    async def call(self, op, **args):
        return await self.manager.call(op, self.profile_id, self.name, **args)

    async def open(self, url, *, persistent=True):
        options = {"restore_url": url} if persistent else {"persistent": False}
        await self.call("open", url=url, **options)

    async def info(self):
        result = await self.call("info")
        if (not isinstance(result, dict) or "generation" not in result
                or not all(isinstance(result.get(key), str) for key in ("url", "title"))):
            raise ManagerError("invalid_response")
        return result

    @staticmethod
    def _challenge(info):
        parts = urlsplit(info["url"])
        title = info["title"].lower()
        return (any(marker in title for marker in ("just a moment", "attention required", "verify you are human"))
                or parts.path.startswith("/cdn-cgi/challenge-platform/")
                or parts.hostname == "challenges.cloudflare.com")

    async def title(self):
        return (await self.info())["title"]

    async def challenge_active(self):
        return self._challenge(await self.info())

    async def challenge_info(self):
        info = await self.info()
        result = await self.call("frames")
        if (not isinstance(result, dict) or not isinstance(result.get("viewport"), dict)
                or not isinstance(result.get("frames"), list)):
            raise ManagerError("invalid_response")
        frames = result["frames"]
        if any(not isinstance(frame, dict) or not isinstance(frame.get("host"), str)
               or type(frame.get("visible")) is not bool
               or (frame.get("box") is not None and not isinstance(frame["box"], dict)) for frame in frames):
            raise ManagerError("invalid_response")
        visible = [frame for frame in frames if frame["visible"] and frame["host"] == "challenges.cloudflare.com"]
        self._viewport = result["viewport"]
        return {"challenge": self._challenge(info) or bool(visible),
                "boxes": [frame["box"] for frame in visible if frame.get("box") is not None],
                "viewport": self._viewport, "failure": False}

    async def click_challenge(self, box, attempt):
        viewport = self._viewport
        x = min(box["x"] + box["width"] - 1, viewport["width"] - 1, box["x"] + self._rng.uniform(26, 28))
        y = min(box["y"] + box["height"] - 1, viewport["height"] - 1, box["y"] + self._rng.uniform(25, 27))
        await self.call("input", kind="click", x=x, y=y, move_steps=self._rng.randint(8, 12),
                        hold_ms=round(self._rng.uniform(100, 200)))

    async def check_site(self):
        info = await self.info()
        if info["url"] == "about:blank":
            await self.call("navigate", url=SITE_URL)
            info = await self.info()
        try:
            parts = urlsplit(info["url"])
        except ValueError:
            raise ManagerError("invalid_response") from None
        path = parts.path
        challenge = self._challenge(info)
        if (parts.scheme, parts.netloc) == ("https", "arkm.com"):
            safe_path = path if path in ("", "/", "/login", "/register") else "/<redacted>"
            self._context = f"page={SITE_URL.rstrip('/')}{safe_path} title="
        else:
            self._context = ""
        if challenge:
            raise Blocked("Browser requires human verification", cause="challenge_timeout")
        if (parts.scheme, parts.netloc) != ("https", "arkm.com"):
            raise ManagerError("unexpected_origin")
        if path in ("/login", "/register"):
            raise AuthRequired("Upstream login required")

    async def failure_context(self):
        return self._context

    async def fetch(self, url, headers):
        result = await self.call("fetch", url=url, headers=headers, origin="https://arkm.com")
        if (not isinstance(result, dict) or type(result.get("status")) is not int
                or not isinstance(result.get("body"), str)
                or any(result.get(key) is not None and not isinstance(result[key], str)
                       for key in ("retry_after", "cf_mitigated"))):
            raise ManagerError("invalid_response")
        return Response(result["status"], result["body"], result.get("retry_after"), result.get("cf_mitigated"))

    async def fill_credentials(self, url, email, password):
        result = await self.call("fill_credentials", url=url, email=email, password=password)
        if not isinstance(result, dict) or type(result.get("filled")) is not bool:
            raise ManagerError("invalid_response")
        return result["filled"]

    async def png(self):
        result = await self.call("screenshot")
        try:
            png = base64.b64decode(result["png"], validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise ManagerError("invalid_response") from None
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ManagerError("invalid_response")
        return png

    async def screenshot(self, path: str):
        Path(path).write_bytes(await self.png())

    async def close(self):
        try:
            await self.call("close")
        except ManagerError as error:
            if error.code != "not_found":
                raise
