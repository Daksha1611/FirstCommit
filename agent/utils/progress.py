"""Progress reporting, so a run can be watched rather than waited on.

RabbitHole threads update_progress(config, "...") through its nodes and streams it
to the frontend. Same idea, and now actually connected: every line goes to the
process-wide event bus, which the gateway serves at /stream and
`pocketchange watch` renders as a live tree. Tracing rides along, so the same
lines land in Langfuse.

Until the funnel was built this collected into a list nobody read. The sink and
the tracing hook were both real; there was simply no consumer. There is now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

Sink = Callable[[str], None]


@dataclass
class Progress:
    """Collects progress lines and forwards them onward as they happen.

    Three destinations, none of which may break a run: the optional sink (a CLI
    printing to a terminal), the event bus (the live stream), and the tracer.
    """

    sink: Sink | None = None
    lines: list[str] = field(default_factory=list)
    # Which node these lines belong to, so the bus can place them in the tree.
    node_id: str = "agent"
    depth: int = 0

    def __call__(self, message: str, **attributes) -> None:
        self.lines.append(message)
        if self.sink:
            self.sink(message)

        try:
            from pocketchange import events

            events.bus.emit(self.node_id, events.SPAWNED, depth=self.depth,
                            description=message, **attributes)
        except Exception:  # noqa: BLE001 - a progress line must never fail a payment
            pass

        if attributes:
            # Best effort: a missing or unconfigured tracer must never break a run.
            try:
                from pocketchange import tracing

                tracing.annotate(**attributes)
            except Exception:  # noqa: BLE001
                pass

    def clear(self) -> None:
        self.lines.clear()
