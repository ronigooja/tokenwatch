import argparse
import asyncio
import signal
from contextlib import suppress
from pathlib import Path

from ..upstream import UpstreamError
from . import BrowserConfig, BrowserUpstreamClient


async def _login(config: BrowserConfig, account_id: str) -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        async with BrowserUpstreamClient(config) as client:
            await client.manual_login(account_id)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main() -> None:
    parser = argparse.ArgumentParser(description="Open the account browser for manual login under X/VNC.")
    parser.add_argument("--account", required=True)
    parser.add_argument("--config", type=Path, default=Path("browser.local.toml"))
    args = parser.parse_args()
    try:
        config = BrowserConfig.from_toml(args.config, signing_key="login-only")
        with suppress(KeyboardInterrupt, asyncio.CancelledError):
            asyncio.run(_login(config, args.account))
    except (ValueError, UpstreamError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
