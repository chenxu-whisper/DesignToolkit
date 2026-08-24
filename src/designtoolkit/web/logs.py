from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from queue import Queue
from threading import Lock

from designtoolkit.web.schemas import LogEventResponse, LogLevel, LogPhase


class LogStore:
    def __init__(self) -> None:
        self._events: dict[str, list[LogEventResponse]] = defaultdict(list)
        self._subscribers: dict[str, list[Queue[LogEventResponse]]] = defaultdict(list)
        self._lock = Lock()

    def append(
        self,
        *,
        task_id: str,
        level: LogLevel,
        phase: LogPhase,
        message: str,
        details: dict[str, object] | None = None,
    ) -> LogEventResponse:
        event = LogEventResponse(
            task_id=task_id,
            timestamp=datetime.now(UTC),
            level=level,
            phase=phase,
            message=message,
            details=dict(details or {}),
        )
        with self._lock:
            self._events[task_id].append(event)
            subscribers = list(self._subscribers[task_id])
        for subscriber in subscribers:
            subscriber.put(event)
        return event

    def history(self, task_id: str) -> list[LogEventResponse]:
        with self._lock:
            return list(self._events.get(task_id, []))

    def history_and_subscribe(
        self,
        task_id: str,
    ) -> tuple[list[LogEventResponse], Queue[LogEventResponse]]:
        subscriber: Queue[LogEventResponse] = Queue()
        with self._lock:
            history = list(self._events.get(task_id, []))
            self._subscribers[task_id].append(subscriber)
        return history, subscriber

    def subscribe(self, task_id: str) -> Queue[LogEventResponse]:
        subscriber: Queue[LogEventResponse] = Queue()
        with self._lock:
            self._subscribers[task_id].append(subscriber)
        return subscriber

    def unsubscribe(
        self,
        task_id: str,
        subscriber: Queue[LogEventResponse],
    ) -> None:
        with self._lock:
            subscribers = self._subscribers.get(task_id, [])
            if subscriber in subscribers:
                subscribers.remove(subscriber)
