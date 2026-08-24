"""The dashboard — a second door, and a window onto the first.

`python -m pocket dashboard` serves one page on 127.0.0.1 that shows what the
terminal cannot: the tape of a turn as it happens, what is in memory, what the
model may call, the tables underneath, and the last release-gate verdict.

Three decisions worth knowing before reading on:

  it is a door       chat here goes through `bus.py`, not around it, so a
                     message typed in the browser lands in the same session as
                     one typed in the terminal and cannot interleave with it
  it renders the     every panel is a projection of something already on disk —
  record, not a      state.db, traces/<date>.jsonl, usage.jsonl,
  second source      eval_report.json. The dashboard has no state of its own,
                     which is why closing it loses nothing
  loopback only      it exposes memory, a SQL browser and a chat box with your
                     tools behind it. It binds 127.0.0.1 and there is no flag to
                     change that; put a real proxy in front if you need one

The page itself is one static file with no build step: open `dashboard.html` and
what you read is what runs.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PAGE = Path(__file__).parent / "dashboard.html"
# The SQL browser reads these and nothing else. An allow-list rather than a
# parser: "which tables may a browser tab see" is a question with a short answer.
BROWSABLE = ("facts", "episodes", "chat_log", "calendar_events", "tasks")
ROW_LIMIT = 200


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


class Panels:
    """Every endpoint is a plain method returning JSON-able data, so the HTTP
    layer below stays a router and nothing else."""

    def __init__(self, pocket, bus):
        self.pocket, self.bus = pocket, bus

    def overview(self) -> dict:
        settings, conn = self.pocket.settings, self.pocket.conn
        report = settings.home / "eval_report.json"
        counts = {t: _rows(conn, f"SELECT COUNT(*) c FROM {t}")[0]["c"] for t in BROWSABLE}
        return {
            "provider": settings.provider, "model": settings.model,
            "small_model": settings.small_model, "home": str(settings.home),
            "spend": self.pocket.tracer.spend(),
            "counts": counts,
            "tools": len(self.pocket.tools.names()),
            "mcp_servers": [s.name for s in self.pocket.mcp_servers],
            "gate": json.loads(report.read_text(encoding="utf-8")) if report.exists() else None,
            "flags": {"graph_workflows": settings.graph_workflows, "team": settings.team,
                      "web": settings.web, "subagents": settings.subagents},
        }

    def transcript(self) -> list[dict]:
        return [m.as_dict() for m in self.bus.transcript]

    def trace(self, limit: int = 200) -> list[dict]:
        return self.pocket.tracer.read()[-limit:]

    def memory(self) -> dict:
        conn = self.pocket.conn
        return {
            "facts": _rows(conn, "SELECT id, subject, content, source FROM facts "
                                 "ORDER BY id DESC LIMIT ?", (ROW_LIMIT,)),
            "episodes": _rows(conn, "SELECT id, summary FROM episodes "
                                    "ORDER BY id DESC LIMIT ?", (ROW_LIMIT,)),
            "skills": [{"name": s.name, "description": s.description, "body": s.body}
                       for s in self.pocket.memory.skills.skills],
            "mirror": str(self.pocket.settings.home / "MEMORY.md"),
        }

    def tools(self) -> list[dict]:
        registry = self.pocket.tools
        return [{"name": name, "origin": registry.get(name).origin,
                 "risk": registry.get(name).risk,
                 "description": registry.get(name).description}
                for name in sorted(registry.names())]

    def data(self, table: str) -> dict:
        if table not in BROWSABLE:
            return {"error": f"'{table}' is not browsable", "tables": list(BROWSABLE)}
        return {"table": table, "tables": list(BROWSABLE),
                "rows": _rows(self.pocket.conn,
                              f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (ROW_LIMIT,))}

    def ops(self) -> dict:
        home = self.pocket.settings.home
        history = home / "eval_runs.jsonl"
        usage = home / "usage.jsonl"

        def lines(path: Path, keep: int) -> list[dict]:
            if not path.exists():
                return []
            return [json.loads(line) for line in
                    path.read_text(encoding="utf-8").splitlines()[-keep:] if line.strip()]

        return {"runs": lines(history, 20), "usage": lines(usage, 100),
                "spend": self.pocket.tracer.spend(), "trace_file": str(self.pocket.tracer.path)}


class _Handler(BaseHTTPRequestHandler):
    panels: Panels
    bus: object

    def log_message(self, *args) -> None:      # the trace is the log
        pass

    # ---- plumbing
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False, default=str).encode(),
                   "application/json; charset=utf-8")

    # ---- routes
    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        routes = {
            "/api/overview": self.panels.overview,
            "/api/transcript": self.panels.transcript,
            "/api/memory": self.panels.memory,
            "/api/tools": self.panels.tools,
            "/api/ops": self.panels.ops,
            "/api/trace": lambda: self.panels.trace(int(query.get("limit", ["200"])[0])),
            "/api/data": lambda: self.panels.data(query.get("table", ["facts"])[0]),
        }
        if url.path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/events":
            return self._stream()
        if url.path in routes:
            try:
                return self._json(routes[url.path]())
            except Exception as exc:              # a broken panel is not a broken server
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        return self._json({"error": "no such route"}, 404)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/chat":
            return self._json({"error": "no such route"}, 404)
        length = int(self.headers.get("Content-Length", "0"))
        try:
            text = json.loads(self.rfile.read(length) or b"{}").get("text", "").strip()
        except json.JSONDecodeError:
            return self._json({"error": "body must be JSON"}, 400)
        if not text:
            return self._json({"error": "nothing to say"}, 400)
        return self._json({"reply": self.bus.submit(text, source="web")})

    def _stream(self) -> None:
        """Server-sent events: the bus tape, forwarded until the tab goes away."""
        pending: queue.Queue = queue.Queue()
        unsubscribe = self.bus.subscribe(lambda kind, event: pending.put(event))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for event in self.bus.replay():
                self._event(event)
            while True:
                try:
                    self._event(pending.get(timeout=15.0))
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")   # proxies drop silent streams
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            unsubscribe()

    def _event(self, event: dict) -> None:
        payload = json.dumps(event, ensure_ascii=False, default=str)
        self.wfile.write(f"data: {payload}\n\n".encode())
        self.wfile.flush()


def serve(pocket, bus, port: int = 7777, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"panels": Panels(pocket, bus), "bus": bus})
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, name="pocket-dashboard",
                     daemon=True).start()
    return server
