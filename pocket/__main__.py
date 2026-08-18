"""The gateway — text in, text out. It moves strings and prints them; every
decision belongs to the agent behind it, which is why a second gateway
(Telegram, voice, a web dock) is a small file and not a rewrite.

    python -m pocket            chat in the terminal
    python -m pocket demo       a scripted tour of the pillars
    python -m pocket eval       the deterministic suite + release gate
    python -m pocket trace      today's trace and the spend ledger
    python -m pocket tools      what the model can call, and what it may do unasked
    python -m pocket mcp        connect the configured MCP servers and prove one call
"""

from __future__ import annotations

import json
import sys

from pocket.agent import Pocket
from pocket.config import load_settings
from pocket.mcp import StdioServer, load_config
from pocket.permissions import cli_confirm


def show(kind: str, event: dict) -> None:
    """The harness, out loud — the same events the tracer writes to disk."""
    if kind == "gate":
        print(f"  gate · {event['decision']} — {event['reason']}")
    elif kind == "triage":
        print(f"  triage · {event['route']} — {event['reason']}")
    elif kind == "route":
        print(f"  route · {event['target']}")
    elif kind == "tool":
        print(f"  tool · {event['tool']}({event['args']})")
        print(f"       -> {event['output']}")
    elif kind == "consolidation":
        print(f"  memory · consolidated {event['new_facts']} new fact(s)")
    elif kind == "compaction":
        print(f"  context · folded {event['messages_folded']} older messages into a summary")
    elif kind == "mcp":
        print(f"  mcp · {event['server']} ({event['mode']}): {', '.join(event['tools'])}")
    elif kind == "mcp_error":
        print(f"  mcp · {event['server']} unavailable — {event['error']} (skipped)")


def chat(pocket: Pocket) -> int:
    print(f"pocket · {pocket.settings.provider}/{pocket.settings.model} · "
          f"state in {pocket.settings.home}/  (ctrl-c to quit)")
    if pocket.settings.provider == "mock":
        print("note: no API key found, so this is the scripted offline model — "
              "it proves the harness runs, not that a model is smart.")
    while True:
        try:
            message = input("\nyou > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not message:
            continue
        if message in ("/quit", "/exit"):
            return 0
        result = pocket.respond(message, observer=show)
        print(f"\npocket > {result.reply}")


DEMO = ["Remember that Alex prefers morning meetings",   # memory write
        "thanks!",                                       # triage: the big model stays asleep
        "what is 2+2?",                                  # the gate skips retrieval
        "Book a catch-up with Alex tomorrow",            # the loop calls a tool
        "What's on my calendar tomorrow?"]               # it reads its own state back


def demo(pocket: Pocket) -> int:
    """Four turns that light up all four pillars in order: memory write, the
    gate skipping, the loop calling a tool, then reading its own state back."""
    for message in DEMO:
        print(f"\nyou > {message}")
        result = pocket.respond(message, observer=show)
        print(f"pocket > {result.reply}")
    spend = pocket.tracer.spend()
    print(f"\n{spend['calls']} model calls · {spend['in']}+{spend['out']} tokens · "
          f"${spend['usd']} (estimated) · trace: {pocket.tracer.path}")
    print(f"memory mirror: {pocket.settings.home / 'MEMORY.md'}")
    return 0


def tools(pocket: Pocket) -> int:
    """Every tool in the prompt, where it came from, and whether it needs a human."""
    for name in sorted(pocket.tools.names()):
        tool = pocket.tools.get(name)
        gate = "asks first" if tool.risk == "ask" else "runs unattended"
        print(f"  {name:<28} {tool.origin:<12} {gate}")
    return 0


DEMO_MCP_CONFIG = {"servers": {"demo": {
    "command": [sys.executable, "-m", "pocket.examples.demo_server"]}}}


def mcp(pocket: Pocket) -> int:
    """Prove the MCP client end to end without involving a model at all."""
    home = pocket.settings.home
    config = load_config(home)
    if not config:
        (home / "mcp.json").write_text(json.dumps(DEMO_MCP_CONFIG, indent=2), encoding="utf-8")
        config = DEMO_MCP_CONFIG["servers"]
        print(f"wrote a starter config to {home / 'mcp.json'} (the bundled demo server)")
    for name, entry in config.items():
        server = StdioServer(name, entry["command"], timeout=entry.get("timeout", 20.0))
        try:
            server.start()
            discovered = server.list_tools()
            print(f"\n  {name} · {server.mode} mode · {len(discovered)} tools")
            for spec in discovered:
                print(f"    {spec['name']:<16} {spec.get('description', '')}")
            if discovered:
                first = discovered[0]["name"]
                print(f"    call {first}(text='hello from pocket') -> "
                      f"{server.call(first, {'text': 'hello from pocket'})}")
        except Exception as exc:
            print(f"  {name} · unavailable: {type(exc).__name__}: {exc}")
        finally:
            server.close()
    return 0


def trace(pocket: Pocket) -> int:
    for line in pocket.tracer.read():
        detail = {k: v for k, v in line.items() if k not in ("event", "ts")}
        print(f"{line['ts'][11:23]}  {line['event']:<12} {detail}")
    print(f"\nspend: {pocket.tracer.spend()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else "chat"
    if command == "eval":                       # no agent needed: it builds its own
        from pocket.evals import main as run_evals

        return run_evals()
    settings = load_settings()
    if command == "demo":
        settings.graph_workflows = True         # show the graph front door too
    pocket = Pocket(settings, confirm=cli_confirm)
    try:
        return {"chat": chat, "demo": demo, "trace": trace,
                "tools": tools, "mcp": mcp}.get(command, chat)(pocket)
    finally:
        pocket.close()


if __name__ == "__main__":
    raise SystemExit(main())
