import asyncio
import logging
import threading

logger = logging.getLogger(__name__)


class Hub:
    """Thread-safe, bounded fan-out for JSON messages."""

    def __init__(self, loop, capacity=64):
        self.loop, self.capacity = loop, capacity
        self.subscribers = set()
        self.lock = threading.Lock()
        self.accounts = {}

    def subscribe(self):
        queue = asyncio.Queue(self.capacity)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self.subscribers.discard(queue)

    def publish(self, kind, data):
        try:
            self.loop.call_soon_threadsafe(self._deliver, {"type": kind, "data": data})
        except Exception:
            logger.exception("WebSocket hub publish failed")

    def _deliver(self, message):
        try:
            for queue in tuple(self.subscribers):
                if any(item["type"] == "resync" for item in queue._queue):
                    continue
                if queue.full():
                    while not queue.empty():
                        queue.get_nowait()
                    queue.put_nowait({"type": "resync", "data": {}})
                else:
                    queue.put_nowait(message)
        except Exception:
            logger.exception("WebSocket hub delivery failed")

    def account(self, data, *, baseline=False):
        try:
            visible = {k: v for k, v in data.items() if k not in
                       ("last_used_at", "available_at", "session_checked_at")}
            with self.lock:
                previous = self.accounts.get(data["id"])
                self.accounts[data["id"]] = visible
                if not baseline and previous != visible:
                    self.publish("account", data)
        except Exception:
            logger.exception("WebSocket account publish failed")

    def deleted(self, account_id):
        try:
            with self.lock:
                self.accounts.pop(account_id, None)
                self.publish("account_deleted", {"id": account_id})
        except Exception:
            logger.exception("WebSocket account deletion publish failed")
