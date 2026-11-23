import asyncio
from contextlib import suppress
from zoneinfo import ZoneInfo

import httpx

from .config import AccountConfig
from .transport import PProxyForwarder


async def detect_timezone(account: AccountConfig) -> str:
    forward = None
    try:
        async with asyncio.timeout(15):
            forward = await PProxyForwarder().start(account)
            async with httpx.AsyncClient(proxy=f"socks5://127.0.0.1:{forward.port}",
                                         timeout=15, trust_env=False) as client:
                response = await client.get("https://ipwho.is/")
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or data.get("success") is not True:
                    raise ValueError
                zone = data["timezone"]["id"]
                if not isinstance(zone, str):
                    raise ValueError
                ZoneInfo(zone)
                return zone
    finally:
        if forward is not None:
            with suppress(Exception):
                await forward.close()
