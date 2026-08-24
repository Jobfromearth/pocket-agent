"""MCP — one client, two transports, in the shape the 2026-07-28 revision asks for.

MCP is how an agent reaches tools it did not ship with. The 2026-07-28 revision
made the protocol **stateless**: the `initialize` / `notifications/initialized`
handshake is gone, and every request instead carries its protocol version,
client identity and capabilities in `_meta`:

    "_meta": {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientInfo": {"name": "pocket", "version": "..."},
      "io.modelcontextprotocol/clientCapabilities": {}
    }

Older servers still speak the handshake, so the client probes with
`server/discover` and treats a failure as "this one is legacy". That probe is the
whole compatibility story, and it is eight lines below.

Two transports, and the split is the reason there is only one of everything else:

  stdio       a child process, JSON-RPC one line at a time. What you use for a
              server you installed
  http        Streamable HTTP: JSON-RPC over POST, answered either as JSON or as
              an SSE stream, with `Mcp-Session-Id` carried when a stateful server
              asks for one. What you use for a server somebody else runs

`Server` holds the protocol — `_meta`, the probe, `tools/list`, `tools/call` —
and knows nothing about pipes or sockets. A transport implements `exchange()`
and `start()`, which is why adding the second one did not touch the first.

Everything a server brings in is namespaced `mcp__<server>__<tool>` and marked
risk="ask": it is third-party code, and the human sees it before it runs.

Spec: modelcontextprotocol/modelcontextprotocol, docs/specification/2026-07-28
(server/discover.mdx, server/tools.mdx, basic/transports/stdio.mdx,
basic/transports/http.mdx).
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from pocket.tools import Tool, ToolRegistry

PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "pocket", "version": "0.3.0"}
# Streamable HTTP answers a POST with either shape, and a client that does not
# say it takes both gets refused by servers that prefer the other one.
ACCEPTS = "application/json, text/event-stream"
SESSION_HEADER = "Mcp-Session-Id"


class MCPError(RuntimeError):
    pass


class Server:
    """The protocol half: JSON-RPC, the version probe, and the two tool calls.

    It knows nothing about how bytes get anywhere. A transport supplies
    `start()` and `exchange()`, and gets all of this for free — which is the
    only reason the HTTP transport below is fifty lines instead of a fork.
    """

    transport = "unknown"

    def __init__(self, name: str, timeout: float = 20.0):
        self.name = name
        self.timeout = timeout
        self.mode = "unknown"          # stateless | legacy
        self._next_id = 0

    # ---- what a transport must provide
    def start(self) -> None:
        raise NotImplementedError

    def exchange(self, payload: dict, expect_reply: bool = True) -> dict | None:
        raise NotImplementedError

    def close(self) -> None:
        return None

    # ---- JSON-RPC
    def _meta(self) -> dict[str, Any]:
        return {"io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
                "io.modelcontextprotocol/clientCapabilities": {}}

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        params = dict(params or {})
        if self.mode != "legacy":      # legacy servers reject the _meta fields
            params["_meta"] = self._meta()
        message = self.exchange({"jsonrpc": "2.0", "id": self._next_id,
                                 "method": method, "params": params})
        if message is None:
            raise MCPError(f"{self.name}: no reply to {method}")
        if "error" in message:
            error = message["error"]
            raise MCPError(f"{self.name}: {method} -> {error.get('code')} {error.get('message')}")
        return message.get("result", {})

    def notify(self, method: str, params: dict | None = None) -> None:
        self.exchange({"jsonrpc": "2.0", "method": method, "params": params or {}},
                      expect_reply=False)

    # ---- lifecycle: the spec's backward-compatibility probe
    def negotiate(self) -> str:
        self.mode = "stateless" if self._probe_stateless() else self._handshake_legacy()
        return self.mode

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


class StdioServer(Server):
    """One MCP server, running as a child process, spoken to in JSON-RPC lines."""

    transport = "stdio"

    def __init__(self, name: str, command: list[str], timeout: float = 20.0,
                 env: dict[str, str] | None = None):
        super().__init__(name, timeout)
        self.command = command
        self.env = env
        self._process: subprocess.Popen | None = None
        self._replies: queue.Queue = queue.Queue()

    def start(self) -> None:
        self._process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=self.env)
        # a reader thread keeps the pipe drained, so a chatty server can never
        # deadlock us behind a full buffer while we wait on one reply
        threading.Thread(target=self._read_loop, daemon=True).start()
        self.negotiate()

    def _read_loop(self) -> None:
        assert self._process and self._process.stdout
        for line in self._process.stdout:
            line = line.strip()
            if line:
                try:
                    self._replies.put(json.loads(line))
                except json.JSONDecodeError:
                    pass               # servers may log to stdout; ignore noise

    def exchange(self, payload: dict, expect_reply: bool = True) -> dict | None:
        assert self._process and self._process.stdin
        self._process.stdin.write(json.dumps(payload) + "\n")
        self._process.stdin.flush()
        if not expect_reply:
            return None
        try:
            while True:
                message = self._replies.get(timeout=self.timeout)
                if message.get("id") == payload["id"]:   # skip notifications/other ids
                    return message
        except queue.Empty as exc:
            raise MCPError(f"{self.name}: no reply to {payload['method']} "
                           f"in {self.timeout}s") from exc

    def close(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()


class HttpServer(Server):
    """Streamable HTTP: the transport for a server somebody else runs.

    Deliberately NOT behind `web.py`'s public-address guard. That guard exists
    because a model can be talked into fetching a URL a web page named; an MCP
    endpoint is a URL YOU wrote into `mcp.json`, and the most ordinary one there
    is `http://127.0.0.1:3000`. Blocking loopback here would block the common
    case to defend against a threat that does not apply.
    """

    transport = "http"

    def __init__(self, name: str, url: str, timeout: float = 20.0,
                 headers: dict[str, str] | None = None):
        super().__init__(name, timeout)
        self.url = url
        self.headers = dict(headers or {})
        self.session: str | None = None

    def start(self) -> None:
        self.negotiate()

    def exchange(self, payload: dict, expect_reply: bool = True) -> dict | None:
        headers = {"Content-Type": "application/json", "Accept": ACCEPTS, **self.headers}
        if self.session:
            headers[SESSION_HEADER] = self.session
        request = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                # a stateful (legacy) server hands out a session on its first
                # answer and expects it back on everything after
                self.session = response.headers.get(SESSION_HEADER) or self.session
                body = response.read().decode("utf-8", errors="replace")
                content_type = response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            raise MCPError(f"{self.name}: HTTP {exc.code} on {payload.get('method')}") from exc
        except OSError as exc:
            raise MCPError(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        if not expect_reply:
            return None
        return self._reply(body, content_type, payload)

    def _reply(self, body: str, content_type: str, payload: dict) -> dict:
        """One POST can be answered as one JSON object or as an SSE stream that
        carries other traffic first, so the id is what picks our answer out."""
        if content_type == "text/event-stream":
            for line in body.splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    message = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if message.get("id") == payload["id"]:
                    return message
            raise MCPError(f"{self.name}: no reply to {payload['method']} in the event stream")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise MCPError(f"{self.name}: {content_type} is not a JSON-RPC reply") from exc

    def close(self) -> None:
        """A stateful server keeps state until told otherwise; a stateless one
        has nothing to end and answers 405, which is not our problem."""
        if not self.session:
            return
        request = urllib.request.Request(
            self.url, headers={SESSION_HEADER: self.session, **self.headers}, method="DELETE")
        try:
            urllib.request.urlopen(request, timeout=5).close()
        except (urllib.error.HTTPError, OSError):
            pass


def build_server(name: str, config: dict) -> Server:
    """`command` means stdio, `url` means HTTP. One key decides, because a config
    that needs a `transport` field is a config with two ways to be wrong."""
    timeout = config.get("timeout", 20.0)
    if config.get("command"):
        return StdioServer(name, config["command"], timeout=timeout, env=config.get("env"))
    if config.get("url"):
        return HttpServer(name, config["url"], timeout=timeout,
                          headers=config.get("headers"))
    raise MCPError(f"{name}: needs either a `command` (stdio) or a `url` (http)")


def load_config(home: Path) -> dict[str, dict]:
    """`.pocket/mcp.json`, the same idea every MCP client uses:

        {"servers": {
            "demo":   {"command": ["python3", "-m", "pocket.examples.demo_server"]},
            "remote": {"url": "https://example.com/mcp",
                       "headers": {"Authorization": "Bearer ..."}}}}
    """
    path = home / "mcp.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("servers", {})
    except (json.JSONDecodeError, OSError):
        return {}


def connect_servers(registry: ToolRegistry, servers: dict[str, dict],
                    notify=None) -> list[Server]:
    """Start each configured server and register its tools. One broken server
    must never cost you the others — or the assistant itself — so every failure
    is reported and skipped."""
    say = notify or (lambda kind, event: None)
    live: list[Server] = []
    for name, config in servers.items():
        try:
            server = build_server(name, config)
        except MCPError as exc:
            say("mcp_error", {"server": name, "error": str(exc)})
            continue
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
        say("mcp", {"server": name, "mode": server.mode, "transport": server.transport,
                    "tools": [t.get("name") for t in tools]})
    return live


def _as_tool(server: Server, spec: dict) -> Tool:
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
