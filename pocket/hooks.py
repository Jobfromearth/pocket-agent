"""Hooks — five named moments in a turn, and the right to interrupt one.

An observer watches. A hook may *change what happens next*, and that difference
is the whole reason this file exists separately from the observer callbacks the
tracer and the dashboard use.

    turn_start      (message)                 -> str | None   refuse the turn
    system_built    (system)                  -> str | None   rewrite the prompt
    before_tool     (name, args)              -> str | None   veto: the string
                                                              becomes the result
    after_tool      (name, args, output)      -> str | None   rewrite the result
    turn_end        (message, reply)          -> None

A hook returning `None` means "no opinion", which is what almost every hook
returns almost every time. The first hook to return a string wins and the rest
are not consulted — a veto that can be overridden by whoever registered last is
not a veto.

The rule that keeps this from becoming a plugin framework: a hook may not raise.
One that does is dropped and the turn continues, because a broken extension must
degrade the assistant, never break it — the same bargain every judge in this
repo makes. `injection.py` is the hook that matters; the mechanism is here so it
is not the only one that can ever exist.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

POINTS = ("turn_start", "system_built", "before_tool", "after_tool", "turn_end")


@dataclass
class Hooks:
    handlers: dict[str, list[Callable]] = field(
        default_factory=lambda: {point: [] for point in POINTS})
    notify: Callable[[str, dict], None] | None = None

    def add(self, point: str, handler: Callable) -> Hooks:
        if point not in POINTS:
            raise ValueError(f"no such hook point '{point}'. Known: {', '.join(POINTS)}")
        self.handlers[point].append(handler)
        return self

    def run(self, point: str, *args) -> str | None:
        """First opinion wins. A handler that raises is dropped, not obeyed."""
        for handler in list(self.handlers.get(point, ())):
            try:
                verdict = handler(*args)
            except Exception as exc:
                self.handlers[point].remove(handler)
                self._say("hook_error", {"point": point, "error": repr(exc)})
                continue
            if isinstance(verdict, str):
                self._say("hook", {"point": point, "handler": getattr(handler, "__name__", "?")})
                return verdict
        return None

    def _say(self, kind: str, event: dict) -> None:
        if self.notify:
            self.notify(kind, event)
