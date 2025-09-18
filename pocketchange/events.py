"""The live view: what the funnel is doing, while it does it.

Distinct from the audit log, and deliberately not merged with it.

    audit.py   durable, hash-linked, the record money is reconciled against.
               Append is part of the transaction. Losing an entry is a bug.

    events.py  ephemeral, in-process, best-effort. A subscriber that is slow or
               absent loses messages and nothing else notices.

Those are different jobs with different failure modes, and a bus that may drop
messages must never become the thing spending is checked against. So the funnel
writes to both: the audit log for what happened, this for what is happening.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Iterator

# The vocabulary. A closed set, because a free-text `kind` becomes unfilterable
# within a week and the CLI has to render each of these differently.
SPAWNED = "spawned"          # a node exists
GRANTED = "granted"          # it received an attenuated token
DECOMPOSED = "decomposed"    # it split into children
SEARCHING = "searching"      # a best-source node is looking
PAYING = "paying"            # a leaf is attempting payment
ALLOWED = "allowed"
DENIED = "denied"
ESCALATED = "escalated"      # held for a human
SETTLED = "settled"          # money moved
BOUND_HIT = "bound_hit"      # a funnel bound stopped the recursion

KINDS = frozenset({
    SPAWNED, GRANTED, DECOMPOSED, SEARCHING, PAYING,
    ALLOWED, DENIED, ESCALATED, SETTLED, BOUND_HIT,
})

TERMINAL = frozenset({ALLOWED, DENIED, ESCALATED, SETTLED, BOUND_HIT, DECOMPOSED})


@dataclass(frozen=True)
class Event:
    """One thing that happened to one node."""

    node_id: str
    parent_id: str | None
    depth: int
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Which run this belongs to.
    #
    # Node ids are tree-local - every run has a "root", a "root.1", a
    # "root.1.2" - so two runs on one gateway emit colliding ids and a console
    # keying by node id draws one tree out of two. That is not a cosmetic
    # overlap: it merges somebody else's spending into your tree. The mandate id
    # is the natural key, since it already identifies the authority the whole
    # run draws on.
    run_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown event kind: {self.kind!r}")

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["at"] = self.at.isoformat()
        return out


class EventBus:
    """Fan-out to zero or more subscribers, none of which can stall the funnel.

    Each subscriber gets its own bounded queue. When a queue fills - a browser
    tab that stopped reading, a CLI someone walked away from - the OLDEST event
    is dropped and a counter goes up. The alternative, blocking the publisher,
    would let a dead subscriber halt a payment run, which is a far worse failure
    than a gap in a progress display.
    """

    def __init__(self, *, history: int = 512, queue_size: int = 512) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[Event]] = []
        self._history: list[Event] = []
        self._history_max = history
        self._queue_size = queue_size
        self.dropped = 0

    def publish(self, event: Event) -> Event:
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._history_max:
                del self._history[: len(self._history) - self._history_max]
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()      # drop the oldest, keep the newest
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):  # pragma: no cover - racy
                    pass
                self.dropped += 1
        return event

    def emit(self, node_id: str, kind: str, *, parent_id: str | None = None,
             depth: int = 0, run_id: str | None = None, **detail: Any) -> Event:
        return self.publish(Event(node_id, parent_id, depth, kind, detail, run_id=run_id))

    def history(self) -> list[Event]:
        with self._lock:
            return list(self._history)

    def subscribe(self) -> queue.Queue[Event]:
        q: queue.Queue[Event] = queue.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[Event]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def stream(self, *, replay: bool = True, timeout: float = 15.0) -> Iterator[Event | None]:
        """Yield events as they arrive; None on idle, so a caller can keep-alive.

        Replays history first by default. A dashboard opened halfway through a
        run should show the tree so far, not an empty screen and a rumour that
        something is happening.
        """
        q = self.subscribe()
        try:
            if replay:
                yield from self.history()
            while True:
                try:
                    yield q.get(timeout=timeout)
                except queue.Empty:
                    yield None
        finally:
            self.unsubscribe(q)


class RemoteBus:
    """Publishes to a gateway's bus over HTTP, for a funnel in another process.

    The funnel usually runs in the agent process and the gateway in its own, so
    an in-process bus means `pocketchange watch` shows an empty screen while a
    run is happening. This closes that.

    Best effort, always: a failed post is swallowed, because a progress line that
    could fail a payment would be worse than no progress line. It keeps a local
    history too, so a caller can still inspect what it tried to send.

    NOTE, said plainly: POST /events is unauthenticated, so anything that can
    reach the gateway can write to the live view. That is acceptable only because
    the stream is explicitly not a record - nothing is reconciled against it, and
    the audit log, which is authenticated and hash-linked, is untouched by it. If
    this stream ever becomes evidence, this endpoint needs a token.
    """

    def __init__(self, url: str, *, timeout: float = 2.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._local = EventBus()
        self.failures = 0

    @property
    def dropped(self) -> int:
        return self._local.dropped

    def publish(self, event: Event) -> Event:
        self._local.publish(event)
        try:
            import httpx

            httpx.post(f"{self.url}/events", json=event.as_dict(), timeout=self.timeout)
        except Exception:  # noqa: BLE001 - never fail a run over a progress line
            self.failures += 1
        return event

    def emit(self, node_id: str, kind: str, *, parent_id: str | None = None,
             depth: int = 0, run_id: str | None = None, **detail: Any) -> Event:
        return self.publish(Event(node_id, parent_id, depth, kind, detail, run_id=run_id))

    def history(self) -> list[Event]:
        return self._local.history()


# One process-wide bus. The gateway streams it, the funnel writes to it, and
# tests build their own rather than sharing this one.
bus = EventBus()
