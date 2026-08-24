"""Ops — a trace is just "what happened, in order", and a ledger of what it cost.

Two files, both append-only, both readable with `cat`:

    .pocket/traces/<date>.jsonl   one line per event: turn_start, gate,
                                    llm, tool, graph_*, turn_end
    .pocket/usage.jsonl           one line per LLM call: model, tokens, $

Tokens are the ground truth; dollars are estimated from a small price table, so
the number shown is honest about being an estimate.

Both files are always written and are the source of truth. When
`OTEL_EXPORTER_OTLP_ENDPOINT` is set, the same events are ALSO mirrored as
OpenTelemetry spans over OTLP/HTTP, with `OTEL_EXPORTER_OTLP_HEADERS` carrying
whatever auth the receiver wants. That is deliberately the whole integration:
Langfuse, Phoenix and every other backend speak this protocol, so a per-vendor
adapter here would be code that exists only to be maintained. A broken exporter
never costs a turn.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from pocket.config import Settings

# What a cached input token costs relative to a fresh one. Anthropic's numbers:
# reading from the cache is a tenth of the price, writing to it is a quarter more
# than not caching at all — which is why a breakpoint on a prefix that changes
# every turn is worse than no breakpoint.
CACHE_READ = 0.1
CACHE_WRITE = 1.25

# USD per 1M tokens (input, output). Estimates for display only.
PRICES = {
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "gpt-5.5": (1.25, 10.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "deepseek-v4-pro": (0.28, 0.42),
    "scripted": (0.0, 0.0),
}


def estimate_cost(model: str, tokens_in: int, tokens_out: int,
                  cached: int = 0, written: int = 0) -> float:
    """`tokens_in` is what the provider billed at full rate; cache reads and
    writes are counted separately because they are priced separately."""
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    return (tokens_in * price_in
            + cached * price_in * CACHE_READ
            + written * price_in * CACHE_WRITE
            + tokens_out * price_out) / 1_000_000


# The exporter is optional (`pip install pocket-agent[tracing]`) and its failure
# is recorded rather than raised — `python -m pocket trace` prints the reason.
OTEL_ERROR = ""


class _Spans:
    """The JSONL trace, mirrored as spans: one `agent_run` per turn, and one
    child per event named `<kind>.<what>`, which is how the waterfall in a
    Phoenix or Langfuse UI ends up shaped like the tape on disk."""

    KINDS: ClassVar[dict[str, str]] = {"llm": "LLM", "tool": "TOOL"}

    def __init__(self, service: str = "pocket"):
        from opentelemetry import trace as otel
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        # HTTP rather than gRPC on purpose: Langfuse Cloud and Phoenix both take
        # OTLP/HTTP with a bearer or basic header, and the SDK reads endpoint and
        # headers straight from the standard OTEL_* variables.
        self._provider = TracerProvider(resource=Resource.create({"service.name": service}))
        self._provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        self._tracer = otel.get_tracer("pocket", tracer_provider=self._provider)
        self._turn = None

    def start_turn(self, message: str) -> None:
        self._turn = self._tracer.start_span("agent_run")
        self._turn.set_attribute("openinference.span.kind", "AGENT")
        self._turn.set_attribute("input.value", message[:2000])

    def event(self, kind: str, payload: dict) -> None:
        what = payload.get("tool") or payload.get("model") or payload.get("decision") or kind
        span = self._tracer.start_span(f"{kind}.{what}")
        span.set_attribute("openinference.span.kind", self.KINDS.get(kind, "CHAIN"))
        for key, value in payload.items():
            span.set_attribute(key, value if isinstance(value, (str, int, float, bool))
                               else json.dumps(value, ensure_ascii=False, default=str)[:2000])
        span.end()

    def end_turn(self) -> None:
        if self._turn is not None:
            self._turn.end()
            self._turn = None
        self._provider.force_flush(2000)      # one turn is one unit of work


def spans_or_none() -> _Spans | None:
    """No endpoint means no exporter, and a missing dependency is not an error
    worth crashing a turn over — the JSONL trace is already on disk either way."""
    global OTEL_ERROR
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return None
    try:
        return _Spans()
    except Exception as exc:
        OTEL_ERROR = f"{type(exc).__name__}: {exc}"
        return None


class Tracer:
    """Also a loop Observer: pass `tracer.event` anywhere an observer goes and
    every step of the turn lands in the trace."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.home / "traces" / f"{datetime.now():%Y-%m-%d}.jsonl"
        self.usage_path = settings.home / "usage.jsonl"
        self.spans = spans_or_none()

    def _write(self, path: Path, record: dict) -> None:
        record["ts"] = datetime.now(UTC).isoformat(timespec="milliseconds")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def event(self, kind: str, payload: dict) -> None:
        self._write(self.path, {"event": kind, **payload})
        if self.spans is not None:
            self.spans.event(kind, payload)
        if kind == "llm":
            usage = payload.get("usage", {})
            model = payload.get("model", self.settings.model)
            tokens_in, tokens_out = usage.get("in", 0), usage.get("out", 0)
            cached, written = usage.get("cached", 0), usage.get("cache_written", 0)
            self._write(self.usage_path, {
                "model": model, "in": tokens_in, "out": tokens_out,
                "cached": cached, "cache_written": written,
                "usd": round(estimate_cost(model, tokens_in, tokens_out, cached, written), 6)})

    @contextmanager
    def turn(self, user_message: str):
        if self.spans is not None:
            self.spans.start_turn(user_message)
        self.event("turn_start", {"message": user_message})
        try:
            yield self
        except Exception as exc:
            self.event("turn_error", {"error": repr(exc)})
            raise
        finally:
            if self.spans is not None:
                self.spans.end_turn()

    def end_turn(self, reply: str, meta: dict) -> None:
        self.event("turn_end", {"reply": reply[:400], **meta})

    # ---- read side: `python -m pocket trace` and the eval suite use these
    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def spend(self) -> dict:
        """`cache_hit` is cached / (cached + fresh input): the share of the prompt
        the provider did not have to read again. It is 0 on providers that do not
        report cache tokens, which is honest — an unknown rate is not a good one."""
        empty = {"calls": 0, "in": 0, "out": 0, "cached": 0, "usd": 0.0, "cache_hit": 0.0}
        if not self.usage_path.exists():
            return empty
        rows = [json.loads(line) for line in
                self.usage_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not rows:
            return empty
        tokens_in = sum(r["in"] for r in rows)
        cached = sum(r.get("cached", 0) for r in rows)
        return {"calls": len(rows), "in": tokens_in,
                "out": sum(r["out"] for r in rows), "cached": cached,
                "usd": round(sum(r["usd"] for r in rows), 4),
                "cache_hit": round(cached / max(1, tokens_in + cached), 3)}
