"""MCP — one stdio client, in the shape the 2026-07-28 revision actually asks for.

MCP is how an agent reaches tools it did not ship with. The 2026-07-28 revision
made the protocol **stateless**: the `initialize` / `notifications/initialized`
handshake is gone, and every request instead carries its protocol version,
client identity and capabilities in `_meta`:

    "_meta": {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientInfo": {"name": "pocket", "version": "..."},
      "io.modelcontextprotocol/clientCapabilities": {}
    }

Older servers still speak the handshake, and on stdio there is no HTTP status
code to fall back on — so the spec's own advice is to probe with `server/discover`
first and treat a failure as "this one is legacy". That probe is the whole
compatibility story, and it is eight lines below.

Everything a server brings in is namespaced `mcp__<server>__<tool>` and marked
risk="ask": it is third-party code, and the human sees it before it runs.

Spec: modelcontextprotocol/modelcontextprotocol, docs/specification/2026-07-28
(server/discover.mdx, server/tools.mdx, basic/transports/stdio.mdx).
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any

from pocket.tools import Tool, ToolRegistry

PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "pocket", "version": "0.2.0"}


class MCPError(RuntimeError):
    pass


class StdioServer:
    """One MCP server, running as a child process, spoken to in JSON-RPC lines."""

    def __init__(self, name: str, command: list[str], timeout: float = 20.0,
                 env: dict[str, str] | None = None):
        self.name = name
        self.command = command
        self.timeout = timeout
        self.env = env
        self.mode = "unknown"          # stateless | legacy
        self._process: subprocess.Popen | None = None
        self._replies: queue.Queue = queue.Queue()
        self._next_id = 0

    # ---- transport
    def start(self) -> None:
        self._process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=self.env)
        # a reader thread keeps the pipe drained, so a chatty server can never
        # deadlock us behind a full buffer while we wait on one reply
        threading.Thread(target=self._read_loop, daemon=True).start()
        self.mode = "stateless" if self._probe_stateless() else self._handshake_legacy()

    def _read_loop(self) -> None:
        assert self._process and self._process.stdout
        for line in self._process.stdout:
            line = line.strip()
            if line:
                try:
                    self._replies.put(json.loads(line))
                except json.JSONDecodeError:
                    pass               # servers may log to stdout; ignore noise

    def close(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()

    # ---- JSON-RPC
    def _meta(self) -> dict[str, Any]:
        return {"io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
                "io.modelcontextprotocol/clientCapabilities": {}}

    def _send(self, payload: dict) -> None:
        assert self._process and self._process.stdin
        self._process.stdin.write(json.dumps(payload) + "\n")
        self._process.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        request_id = self._next_id
        params = dict(params or {})
        if self.mode != "legacy":      # legacy servers reject the _meta fields
            params["_meta"] = self._meta()
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            while True:
                message = self._replies.get(timeout=self.timeout)
                if message.get("id") == request_id:   # skip notifications/other ids
                    break
        except queue.Empty as exc:
            raise MCPError(f"{self.name}: no reply to {method} in {self.timeout}s") from exc
        if "error" in message:
            error = message["error"]
            raise MCPError(f"{self.name}: {method} -> {error.get('code')} {error.get('message')}")
        return message.get("result", {})

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # ---- lifecycle: the spec's stdio backward-compatibility probe
    def _probe_stateless(self) -> bool:
        """`server/discover` is mandatory in 2026-07-28 and absent before it, so
        one call tells us which dialect this server speaks."""
        try:
            result = self.request("server/discover")
        except MCPError:
            return False
        versions = result.get("supportedVersions") or []
        return not versions or PROTOCOL_VERSION in versions

    def _handshake_legacy(self) -> str:
        self.mode = "legacy"
        self.request("initialize", {"protocolVersion": LEGACY_PROTOCOL_VERSION,
                                    "capabilities": {}, "clientInfo": CLIENT_INFO})
        self.notify("notifications/initialized")
        return "legacy"

    # ---- tools
    def list_tools(self) -> list[dict]:
        return self.request("tools/list").get("tools", [])

    def call(self, tool: str, arguments: dict) -> str:
        result = self.request("tools/call", {"name": tool, "arguments": arguments})
        if result.get("resultType") == "input_required":
            # multi round-trip requests: the server wants a human-supplied value.
            # Honest refusal beats a silent half-answer.
            return (f"{self.name}: '{tool}' needs extra input this client cannot supply yet "
                    f"(MCP multi round-trip request).")
        text = "\n".join(block.get("text", "") for block in result.get("content", [])
                         if block.get("type") == "text")
        if result.get("isError"):
            return f"Error from {self.name}.{tool}: {text}"
        return text or "(no text content)"


def load_config(home: Path) -> dict[str, dict]:
    """`.pocket/mcp.json`, the same idea every MCP client uses:

        {"servers": {"demo": {"command": ["python3", "-m", "pocket.examples.demo_server"]}}}
    """
    path = home / "mcp.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("servers", {})
    except (json.JSONDecodeError, OSError):
        return {}


def connect_servers(registry: ToolRegistry, servers: dict[str, dict],
                    notify=None) -> list[StdioServer]:
    """Start each configured server and register its tools. One broken server
    must never cost you the others — or the assistant itself — so every failure
    is reported and skipped."""
    say = notify or (lambda kind, event: None)
    live: list[StdioServer] = []
    for name, config in servers.items():
        server = StdioServer(name, config["command"], timeout=config.get("timeout", 20.0))
        try:
            server.start()
            tools = server.list_tools()
        except Exception as exc:
            say("mcp_error", {"server": name, "error": f"{type(exc).__name__}: {exc}"})
            server.close()
            continue
        for spec in tools:
            registry.register(_as_tool(server, spec))
        live.append(server)
        say("mcp", {"server": name, "mode": server.mode,
                    "tools": [t.get("name") for t in tools]})
    return live


def _as_tool(server: StdioServer, spec: dict) -> Tool:
    tool_name = spec["name"]

    def run(**arguments) -> str:
        return server.call(tool_name, arguments)

    return Tool(
        name=f"mcp__{server.name}__{tool_name}",
        description=f"[{server.name} via MCP] {spec.get('description', '')}".strip(),
        input_schema=spec.get("inputSchema") or {"type": "object", "properties": {}},
        fn=run,
        risk="ask",                    # third-party code: a human sees it first
        origin=f"mcp:{server.name}")
