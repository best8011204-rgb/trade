"""스레드 경계를 넘는 유일한 통로 — queue.Queue 기반 pub/sub.

백그라운드(엔진/웹소켓) 스레드는 publish()만 호출한다 (큐 적재라 스레드세이프).
위젯 콜백은 메인 스레드에서 dispatch()가 큐를 비우면서 호출한다 — Tkinter
위젯은 메인 스레드에서만 건드려야 하므로, dispatch()는 반드시 root.after()
루프 등 메인 스레드에서만 불러야 한다.
"""

import queue
import sys
from collections import defaultdict


class EventBus:
    def __init__(self):
        self._queue = queue.Queue()
        self._subscribers = defaultdict(list)

    def subscribe(self, topic, callback):
        self._subscribers[topic].append(callback)

    def publish(self, topic, payload):
        self._queue.put((topic, payload))

    def dispatch(self, max_events=500):
        """큐에 쌓인 이벤트를 구독자에게 전달한다. 메인 스레드에서만 호출할 것."""
        n = 0
        while n < max_events:
            try:
                topic, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            for callback in self._subscribers.get(topic, ()):
                try:
                    callback(payload)
                except Exception as e:
                    print(f"[EventBus] subscriber error on '{topic}': {e}", file=sys.stderr)
            n += 1
