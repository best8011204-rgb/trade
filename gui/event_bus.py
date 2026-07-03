"""스레드 간 이벤트 전달 브리지.

백그라운드 스레드(엔진 러너)는 publish()만 호출한다. 실제 구독자 콜백은
반드시 메인(GUI) 스레드에서만 실행되어야 tkinter 위젯을 안전하게 건드릴 수
있으므로, publish()는 내부 queue.Queue에 적재만 하고 dispatch()가 메인
스레드에서 큐를 비우며 콜백을 호출한다. (1번 자료의 queue 폴링 패턴을
토픽 기반 구독 모델로 일반화한 것)
"""

import queue
from collections import defaultdict


class EventBus:
    def __init__(self):
        self._queue = queue.Queue()
        self._subscribers = defaultdict(list)

    def subscribe(self, topic, callback):
        """콜백은 항상 메인 스레드에서 dispatch()를 통해 호출된다."""
        self._subscribers[topic].append(callback)

    def publish(self, topic, data=None):
        """어느 스레드에서나 호출 가능. 큐에 적재만 하고 즉시 반환한다."""
        self._queue.put((topic, data))

    def dispatch(self):
        """메인 스레드에서 주기적으로(root.after) 호출. 큐를 비우며 구독자에게 전달한다."""
        while True:
            try:
                topic, data = self._queue.get_nowait()
            except queue.Empty:
                break
            for callback in self._subscribers.get(topic, ()):
                callback(data)
