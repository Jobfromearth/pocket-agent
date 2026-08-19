"""The gateway — text in, text out. It moves strings and prints them; every
decision belongs to the agent behind it, which is why a second gateway
(Telegram, voice, a web dock) is a small file and not a rewrite.

    python -m pocket            chat in the terminal
    python -m pocket demo       a scripted tour of the pillars
    python -m pocket eval       the deterministic suite + release gate
    python -m pocket trace      today's trace and the spend ledger
    python -m pocket tools      what the model can call, and what it may do unasked
    python -m pocket mcp        connect the configured MCP servers and prove one call
    python -m pocket team       run a three-worker plan over the shared board

Inside a chat, a line starting with `/` never reaches a model — see COMMANDS.
"""

from __future__ import annotations

import json
import sys

from pocket.agent import Pocket, compose
from pocket.config import load_settings
from pocket.mcp import StdioServer, load_config
from pocket.permissions import cli_confirm
from pocket.team import run_team


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
    elif kind in ("node_start", "node_end"):
        label = "worker" if event.get("workflow", "").startswith("team:") else "node"
        if kind == "node_start":
            print(f"  {label} · {event['node']} started")
        else:
            state = f"failed — {event['error']}" if event.get("error") else "done"
            print(f"  {label} · {event['node']} {state} ({event['ms']}ms)")


COMMANDS = """\
  /help      this list
  /tools     every tool in the prompt, and whether it asks first
  /context   what goes into the next prompt, and how close it is to its budget
  /memory    what is remembered, and where the mirror on disk is
  /board     every team task this database has run, newest team first
  /new       start a fresh conversation (memory, board and traces are kept)
  /quit      leave"""


def command(pocket: Pocket, message: str) -> str | None:
    """Handle a slash command locally, or return None when the line belongs to
    the model. Nanobot's terminal client is where the idea comes from: the
    things you want to know mid-conversation — what is in context, what can run,
    what was remembered — are answers the harness already has, so none of them
    should cost a model call."""
    if not message.startswith("/"):
        return None
    verb = message.split()[0]
    if verb == "/help":
        return COMMANDS
    if verb == "/tools":
        return "\n".join(
            f"  {name:<28} {pocket.tools.get(name).origin:<12} "
            f"{'asks first' if pocket.tools.get(name).risk == 'ask' else 'runs unattended'}"
            for name in sorted(pocket.tools.names()))
    if verb == "/context":
        window = pocket.session.messages_for("")
        used = sum(len(str(m["content"])) for m in window)
        budget = pocket.settings.context_budget_chars
        lines = [(f"  history · {len(pocket.session.history) // 2} turns kept, "
                  f"window is the last {pocket.settings.history_turns}"),
                 f"  prompt  · {used}/{budget} chars of budget ({used * 100 // max(budget, 1)}%)",
                 f"  tools   · {len(pocket.tools.names())} registered, all of them in every prompt"]
        if used > budget:
            lines.append("  next turn folds the oldest messages into one summary")
        return "\n".join(lines)
    if verb == "/memory":
        counts = {table: pocket.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
                  for table in ("facts", "episodes", "chat_log")}
        return (f"  facts {counts['facts']} · episodes {counts['episodes']} · "
                f"messages {counts['chat_log']} · skills {len(pocket.memory.skills.skills)}\n"
                f"  mirror: {pocket.settings.home / 'MEMORY.md'} (source of truth: state.db)")
    if verb == "/board":
        rows = pocket.conn.execute(
            "SELECT team, key, status, result FROM tasks ORDER BY id DESC LIMIT 20").fetchall()
        if not rows:
            return "  no team has run yet — POCKET_TEAM=1 registers assign_team"
        return "\n".join(f"  {r['team']}  {r['status']:<8} [{r['key']}] "
                          f"{(r['result'] or '')[:60]}" for r in rows)
    if verb == "/new":
        pocket.session.history.clear()
        return "  fresh conversation — state.db still has every message"
    return f"  unknown command {verb}. /help lists them."


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
        handled = command(pocket, message)
        if handled is not None:
            print(handled)
            continue
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


DEMO_PLAN = [
    {"key": "remember", "task": "Remember that the Q4 offsite is on Friday",
     "tools": "save_note"},
    {"key": "book", "task": "Schedule a kickoff with Alex tomorrow at 9am",
     "tools": "create_event"},
    {"key": "confirm", "task": "What is on my calendar tomorrow?",
     "tools": "list_events", "needs": "book"},
]


def team(pocket: Pocket) -> int:
    """Three workers over one board, without a model deciding anything: two are
    independent and run in the same wave, the third waits for `book` and is
    handed its result. The board it leaves behind is a table in state.db."""
    print(f"goal · prepare the kickoff  ({len(DEMO_PLAN)} tasks, "
          f"{sum(1 for t in DEMO_PLAN if not t.get('needs'))} of them independent)\n")
    board = run_team(pocket.client, pocket.settings.model, pocket.tools, pocket.conn,
                     goal="prepare the kickoff", plan=DEMO_PLAN,
                     max_iterations=max(2, pocket.settings.max_iterations // 2),
                     observer=compose(show, pocket.tracer.event))
    print(f"\n{board.render()}")
    print(f"\nthe board is a table: sqlite3 {pocket.settings.home / 'state.db'} "
          f"\"select key, status from tasks where team='{board.team}'\"")
    return 0


def trace(pocket: Pocket) -> int:
    for line in pocket.tracer.read():
        detail = {k: v for k, v in line.items() if k not in ("event", "ts")}
        print(f"{line['ts'][11:23]}  {line['event']:<12} {detail}")
    print(f"\nspend: {pocket.tracer.spend()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    verb = argv[0] if argv else "chat"
    if verb == "eval":                          # no agent needed: it builds its own
        from pocket.evals import main as run_evals

        return run_evals()
    settings = load_settings()
    if verb == "demo":
        settings.graph_workflows = True         # show the graph front door too
    if verb in ("team", "tools"):
        settings.team = True                    # so assign_team is visible in both
    pocket = Pocket(settings, confirm=cli_confirm)
    try:
        return {"chat": chat, "demo": demo, "trace": trace, "team": team,
                "tools": tools, "mcp": mcp}.get(verb, chat)(pocket)
    finally:
        pocket.close()


if __name__ == "__main__":
    raise SystemExit(main())
