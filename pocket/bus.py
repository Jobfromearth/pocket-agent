"""The bus — many doors, one conversation.

A gateway's job is to move strings. The moment there is a second one, three
questions appear that a gateway must not be allowed to answer for itself:

  who is talking       the terminal, a browser tab and a chat app are doors into
                       the SAME assistant, not three assistants with three
                       memories. Every message carries its `source` for the
                       record, and they all land in one `Session`
  what if two arrive   turns are serialised by a single worker thread. Two doors
    at once            cannot interleave into one context window, and nothing
                       needs a lock because nothing else touches the assistant
  who else is watching a turn started from Telegram still streams its gate
                       decision and tool calls to an open dashboard, because
                       events are published to every subscriber, not returned to
                       the caller

So a gateway is `submit()` plus, if it wants to render progress, `subscribe()`.
That is the whole contract, and it is why `dashboard.py` and `telegram.py` are
small files instead of forks of `__main__.py`.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# How many recent events a door that opens late gets to replay. Small on
# purpose: the trace on disk is the record, this is only what the UI needs to
# not look empty when you first load it.
REPLAY = 200


@dataclass
class Message:
    role: str                    # user | assistant
    text: str
    source: str                  # cli | web | telegram | ...
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "text": self.text, "source": self.source, "at": self.at}


@dataclass
class _Job:
    text: str
    source: str
    future: Future


class Bus:
    """One worker, one assistant, any number of doors."""

    def __init__(self, pocket):
        self.pocket = pocket
        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._subscribers: list[Callable[[str, dict], None]] = []
        self._lock = threading.Lock()
        self._recent: list[dict] = []
        self.transcript: list[Message] = []
        self._worker: threading.Thread | None = None

    # ---- doors --------------------------------------------------------------
    def submit(self, text: str, source: str = "cli", timeout: float = 300.0) -> str:
        """Hand one message to the assistant and wait for the reply. A gateway
        that would rather not block can keep the Future from `submit_async`."""
        return self.submit_async(text, source).result(timeout=timeout)

    def submit_async(self, text: str, source: str = "cli") -> Future:
        job = _Job(text=text, source=source, future=Future())
        self._jobs.put(job)
        self.publish("queued", {"source": source, "depth": self._jobs.qsize()})
        return job.future

    def subscribe(self, listener: Callable[[str, dict], None]) -> Callable[[], None]:
        """Returns the unsubscribe. A listener that raises is dropped rather than
        allowed to take a turn down with it — a dashboard is not load-bearing."""
        with self._lock:
            self._subscribers.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._subscribers:
                    self._subscribers.remove(listener)

        return unsubscribe

    def replay(self) -> list[dict]:
        with self._lock:
            return list(self._recent)

    # ---- the tape -----------------------------------------------------------
    def publish(self, kind: str, event: dict) -> None:
        record = {"event": kind, "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
                  **event}
        with self._lock:
            self._recent.append(record)
            del self._recent[:-REPLAY]
            listeners = list(self._subscribers)
        for listener in listeners:
            try:
                listener(kind, record)
            except Exception:
                self.unsubscribe_quietly(listener)

    def unsubscribe_quietly(self, listener) -> None:
        with self._lock:
            if listener in self._subscribers:
                self._subscribers.remove(listener)

    # ---- the worker ---------------------------------------------------------
    def start(self) -> Bus:
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="pocket-bus", daemon=True)
            self._worker.start()
        return self

    def stop(self) -> None:
        if self._worker is not None:
            self._jobs.put(None)
            self._worker.join(timeout=5.0)
            self._worker = None

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            self._serve(job)

    def _serve(self, job: _Job) -> None:
        """One turn, start to finish. Every failure becomes an assistant message:
        a door that asked a question deserves an answer, even when the answer is
        that something broke."""
        self._remember(Message("user", job.text, job.source))
        self.publish("turn_start", {"source": job.source, "message": job.text})
        try:
            result = self.pocket.respond(
                job.text, observer=lambda kind, event: self.publish(kind, event))
            reply = result.reply
        except Exception as exc:
            reply = f"Something broke on the way to an answer: {type(exc).__name__}: {exc}"
            self.publish("turn_error", {"source": job.source, "error": repr(exc)})
        self._remember(Message("assistant", reply, job.source))
        self.publish("turn_reply", {"source": job.source, "reply": reply})
        job.future.set_result(reply)

    def _remember(self, message: Message) -> None:
        with self._lock:
            self.transcript.append(message)
        self.publish("message", message.as_dict())
