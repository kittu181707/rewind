"""One in-process run, with a separate exact-gate channel for browser choices."""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Condition, Thread
from uuid import uuid4

from rewind.events import RunEvent


@dataclass
class PendingGate:
    id: str
    decision: str | None = None


class LiveRun:
    def __init__(self, timeout: float = 600) -> None:
        self.timeout = timeout
        self.lock = Condition()
        self.events: deque[tuple[int, RunEvent]] = deque(maxlen=512)
        self.sequence = 0
        self.subscribers = 0
        self.active = False
        self.stopped = False
        self.preview: dict | None = None
        self.pending: PendingGate | None = None
        self.last_gate: str | None = None
        self.report: Path | None = None

    def _publish(self, event: RunEvent) -> None:
        self.sequence += 1
        self.events.append((self.sequence, event))
        self.lock.notify_all()

    def observe(self, event: RunEvent) -> None:
        with self.lock:
            if event.type == "gate":
                self.preview = event.data
            else:
                self._publish(event)

    def messages(self, after: int) -> list[tuple[int, RunEvent]]:
        with self.lock:
            return [(index, event) for index, event in self.events if index > after]

    def subscribe(self) -> None:
        with self.lock:
            self.subscribers += 1

    def disconnect(self) -> None:
        with self.lock:
            self.subscribers -= 1
            if not self.subscribers:
                self.stop()

    def stop(self) -> None:
        with self.lock:
            if self.active:
                self.stopped = True
                self.lock.notify_all()

    def start(self, work: Callable[[], None]) -> bool:
        with self.lock:
            if self.active or not self.subscribers:
                return False
            self.active = True
            self.stopped = False
            self.preview = None
            self.pending = None
            self.report = None
            self.events.clear()
            self._publish(RunEvent("started", {}))
            Thread(target=work, name="rewind-ui-run", daemon=True).start()
            return True

    def wait_for_gate(self, _prompt: str) -> str:
        with self.lock:
            if self.preview is None or self.stopped:
                return "no"
            details, self.preview = self.preview, None
            gate = PendingGate(uuid4().hex)
            self.pending = gate
            self._publish(RunEvent("gate", {**details, "id": gate.id}))
            self.lock.wait_for(lambda: self.stopped or gate.decision is not None, timeout=self.timeout)
            approved = gate.decision == "approve" and not self.stopped
            self.pending = None
            self.last_gate = gate.id
            self._publish(RunEvent("decision", {"id": gate.id, "approved": approved}))
            return "yes" if approved else "no"

    def decide(self, gate_id: str, decision: str) -> bool | None:
        with self.lock:
            if self.last_gate == gate_id:
                return False
            gate = self.pending
            if gate is None or gate.id != gate_id:
                return None
            if gate.decision is not None or self.stopped:
                return False
            gate.decision = decision
            self.lock.notify_all()
            return True

    def finish(self, event: RunEvent, report: Path | None = None) -> None:
        with self.lock:
            self.report = report
            self._publish(event)
            self.pending = None
            self.active = False
