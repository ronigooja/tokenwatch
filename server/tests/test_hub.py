import asyncio
import threading

from tokenwatch.hub import Hub


def test_hub_overflow_and_thread():
    async def run():
        hub = Hub(asyncio.get_running_loop(), capacity=2)
        slow, fast = hub.subscribe(), hub.subscribe()
        thread = threading.Thread(target=hub.publish, args=("job", {"id": 1}))
        thread.start()
        thread.join()
        await asyncio.sleep(0)
        assert await fast.get() == {"type": "job", "data": {"id": 1}}
        hub.publish("job", {"id": 2})
        await asyncio.sleep(0)
        assert (await fast.get())["data"] == {"id": 2}
        hub.publish("job", {"id": 3})
        await asyncio.sleep(0)
        assert await slow.get() == {"type": "resync", "data": {}}
        assert (await fast.get())["data"] == {"id": 3}
        hub.unsubscribe(slow)
        hub.unsubscribe(fast)
        assert not hub.subscribers
    asyncio.run(run())
