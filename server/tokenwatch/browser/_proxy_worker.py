import asyncio
import json
import os
import sys


async def _serve() -> None:
    from pproxy import server

    host, port, user, password = json.loads(sys.stdin.buffer.readline())
    authority = f"[{host}]" if ":" in host else host
    upstream = server.proxy_by_uri(f"socks5://{authority}:{port}", server.DIRECT)
    upstream.users = [f"{user}:{password}".encode()]
    listener = server.proxy_by_uri("socks5://127.0.0.1:0", server.DIRECT)
    listener.port = 0

    class _NoDirect:
        def logtext(self, host, port):
            return ""

        async def open_connection(self, *args):
            raise ConnectionError("No upstream proxy available")

    server.DIRECT = _NoDirect()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_reader(sys.stdin.fileno(), lambda: (os.read(sys.stdin.fileno(), 1), stopped.set()))
    async with await listener.start_server({"rserver": [upstream]}) as bound:
        print(bound.sockets[0].getsockname()[1], flush=True)
        await stopped.wait()


if __name__ == "__main__":
    asyncio.run(_serve())
