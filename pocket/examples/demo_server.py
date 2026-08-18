"""A tiny MCP server, stdlib only — so `pocket` can demo MCP with no install.

Speaks the 2026-07-28 stateless revision by default (`server/discover`, no
handshake, `_meta` on every request). Run it with `--legacy` and it refuses
`server/discover` and demands the old `initialize` handshake instead, which is
how the client's backward-compatibility probe gets exercised in the eval suite.

    python3 -m pocket.examples.demo_server          # modern, stateless
    python3 -m pocket.examples.demo_server --legacy # pre-2026 handshake
"""

from __future__ import annotations

import json
import sys

PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {"name": "word_count",
     "description": "Count the words and characters in a piece of text.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "shout",
     "description": "Return the text in upper case, for testing an MCP round trip.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
]


def run_tool(name: str, arguments: dict) -> dict:
    text = arguments.get("text", "")
    if name == "word_count":
        body = f"{len(text.split())} words, {len(text)} characters"
    elif name == "shout":
        body = text.upper()
    else:
        return {"resultType": "complete", "isError": True,
                "content": [{"type": "text", "text": f"unknown tool '{name}'"}]}
    return {"resultType": "complete", "isError": False,
            "content": [{"type": "text", "text": body}]}


def main(argv: list[str]) -> int:
    legacy = "--legacy" in argv
    initialized = not legacy
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method, request_id = message.get("method"), message.get("id")
        if request_id is None:                       # a notification
            if method == "notifications/initialized":
                initialized = True
            continue

        if method == "server/discover" and not legacy:
            result = {"resultType": "complete", "supportedVersions": [PROTOCOL_VERSION],
                      "capabilities": {"tools": {}},
                      "_meta": {"io.modelcontextprotocol/serverInfo":
                                {"name": "pocket-demo", "version": "1.0.0"}},
                      "ttlMs": 3600000, "cacheScope": "public"}
        elif method == "initialize" and legacy:
            result = {"protocolVersion": LEGACY_PROTOCOL_VERSION,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "pocket-demo-legacy", "version": "1.0.0"}}
        elif method == "tools/list" and initialized:
            result = {"resultType": "complete", "tools": TOOLS,
                      "ttlMs": 300000, "cacheScope": "public"}
        elif method == "tools/call" and initialized:
            params = message.get("params", {})
            result = run_tool(params.get("name", ""), params.get("arguments", {}))
        else:
            reply = {"jsonrpc": "2.0", "id": request_id,
                     "error": {"code": -32601, "message": f"method not found: {method}"}}
            print(json.dumps(reply), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
