"""Ops — a trace is just "what happened, in order", and a ledger of what it cost.

Two files, both append-only, both readable with `cat`:

    .pocket/traces/<date>.jsonl   one line per event: turn_start, gate,
                                    llm, tool, graph_*, turn_end
    .pocket/usage.jsonl           one line per LLM call: model, tokens, $

Tokens are the ground truth; dollars are estimated from a small price table, so
the number shown is honest about being an estimate.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pocket.config import Settings

# USD per 1M tokens (input, output). Estimates for display only.
PRICES = {
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "gpt-5.5": (1.25, 10.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "deepseek-v4-pro": (0.28, 0.42),
    "scripted": (0.0, 0.0),
}


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000


class Tracer:
    """Also a loop Observer: pass `tracer.event` anywhere an observer goes and
    every step of the turn lands in the trace."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.home / "traces" / f"{datetime.now():%Y-%m-%d}.jsonl"
        self.usage_path = settings.home / "usage.jsonl"

    def _write(self, path: Path, record: dict) -> None:
        record["ts"] = datetime.now(UTC).isoformat(timespec="milliseconds")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def event(self, kind: str, payload: dict) -> None:
        self._write(self.path, {"event": kind, **payload})
        if kind == "llm":
            usage = payload.get("usage", {})
            model = payload.get("model", self.settings.model)
            self._write(self.usage_path, {
                "model": model, "in": usage.get("in", 0), "out": usage.get("out", 0),
                "usd": round(estimate_cost(model, usage.get("in", 0), usage.get("out", 0)), 6)})

    @contextmanager
    def turn(self, user_message: str):
        self.event("turn_start", {"message": user_message})
        try:
            yield self
        except Exception as exc:
            self.event("turn_error", {"error": repr(exc)})
            raise

    def end_turn(self, reply: str, meta: dict) -> None:
        self.event("turn_end", {"reply": reply[:400], **meta})

    # ---- read side: `python -m pocket trace` and the eval suite use these
    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def spend(self) -> dict:
        if not self.usage_path.exists():
            return {"calls": 0, "in": 0, "out": 0, "usd": 0.0}
        rows = [json.loads(line) for line in
                self.usage_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return {"calls": len(rows), "in": sum(r["in"] for r in rows),
                "out": sum(r["out"] for r in rows), "usd": round(sum(r["usd"] for r in rows), 4)}
