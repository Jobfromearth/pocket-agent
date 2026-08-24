"""The gateway — text in, text out. It moves strings and prints them; every
decision belongs to the agent behind it, which is why a second gateway is a
small file and not a rewrite: they all meet at `bus.py`.

    python -m pocket            chat in the terminal
    python -m pocket dashboard  the browser door, and the terminal one, sharing a bus
    python -m pocket telegram   the chat door (TELEGRAM_BOT_TOKEN + POCKET_TELEGRAM_ALLOW)
    python -m pocket demo       a scripted tour of the pillars
    python -m pocket eval       the deterministic suite, offline and free
    python -m pocket judge      the scored suite: reply quality and gate accuracy
    python -m pocket gate       both suites + the verdict CI reads
    python -m pocket trace      today's trace and the spend ledger
    python -m pocket tools      what the model can call, and what it may do unasked
    python -m pocket mcp        connect the configured MCP servers and prove one call
    python -m pocket team       run a three-worker plan over the shared board

Inside a chat, a line starting with `/` never reaches a model — see COMMANDS.
"""

from __future__ import annotations

import json
import os
import sys

from pocket import dream
from pocket.agent import Pocket, compose
from pocket.bus import Bus
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
    elif kind == "coder_start":
        print(f"  coder · {event['coder']} started in {event['cwd']}")
    elif kind == "coder_progress":
        detail = f" {event['detail']}" if event.get("detail") else ""
        print(f"  coder · [{event['line']}] {event.get('event') or event.get('note', '')}{detail}")
    elif kind == "coder_end":
        files = ", ".join(event["created"]) or "no new files"
        print(f"  coder · exit {event['exit_code']} — {files}")
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
  /dream     consolidate now, without waiting for the exchange count
  /dream-log [sha]   what consolidation has decided, or one run in detail
  /dream-restore <sha>   retract exactly the facts one run added
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
    if verb == "/dream":
        added = pocket.memory.maybe_consolidate(notify=show)
        pocket.memory.export_markdown()
        if not added:
            return ("  nothing to consolidate yet — it needs "
                    f"{pocket.settings.consolidate_every} exchanges it has not read.")
        return f"  distilled {added} fact(s).\n{dream.render(pocket.conn, limit=1)}"
    if verb == "/dream-log":
        rest = message.split(maxsplit=1)
        return (dream.show(pocket.conn, rest[1]) if len(rest) > 1
                else dream.render(pocket.conn))
    if verb == "/dream-restore":
        rest = message.split(maxsplit=1)
        if len(rest) < 2:
            return "  usage: /dream-restore <sha>   (/dream-log lists them)"
        answer = dream.restore(pocket.conn, rest[1])
        pocket.memory.export_markdown()
        return answer
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


def chat(pocket: Pocket, bus: Bus | None = None) -> int:
    """The terminal door. With a bus it is one door among several — a reply
    typed here and one typed in the browser land in the same session, in the
    order the bus received them."""
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
        if bus is None:
            reply = pocket.respond(message, observer=show).reply
        else:
            reply = bus.submit(message, source="cli")
        print(f"\npocket > {reply}")


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
          f"{int(spend['cache_hit'] * 100)}% of the prompt came from cache · "
          f"${spend['usd']} (estimated) · trace: {pocket.tracer.path}")
    print(f"memory mirror: {pocket.settings.home / 'MEMORY.md'}")
    return 0


def tools(pocket: Pocket) -> int:
    """Every tool in the prompt, where it came from, and whether it needs a human.

    This subcommand turns `assign_team` on so you can read it, which means the
    listing is one tool longer than a real chat unless POCKET_TEAM=1. Saying so
    is cheaper than a listing that quietly disagrees with the prompt."""
    for name in sorted(pocket.tools.names()):
        tool = pocket.tools.get(name)
        gate = "asks first" if tool.risk == "ask" else "runs unattended"
        flag = "  (POCKET_TEAM=1 only)" if name == "assign_team" else ""
        print(f"  {name:<28} {tool.origin:<12} {gate}{flag}")
    if not os.getenv("POCKET_TEAM"):
        print("\n  note: assign_team is shown for reference; it is NOT in the prompt "
              "of a normal chat.\n  Start with POCKET_TEAM=1 to give the model a team.")
    return 0


def dashboard(pocket: Pocket) -> int:
    """Two doors on one bus: a page on 127.0.0.1 and this terminal. Whatever you
    type in either shows up in both, because neither of them owns the session."""
    from pocket.dashboard import serve

    bus = Bus(pocket).start()
    port = int(os.getenv("POCKET_DASHBOARD_PORT", "7777"))
    server = serve(pocket, bus, port=port)
    # the CLI door prints the same events the page renders, so a turn started in
    # the browser is still legible to someone watching the terminal
    bus.subscribe(lambda kind, event: show(kind, event) if kind != "message" else None)
    print(f"dashboard · http://127.0.0.1:{port}  (loopback only, ctrl-c to quit)")
    try:
        return chat(pocket, bus=bus)
    finally:
        server.shutdown()
        bus.stop()


def telegram(pocket: Pocket) -> int:
    """The third door. It shares everything with the other two except the wire."""
    from pocket.telegram import run

    bus = Bus(pocket).start()
    try:
        return run(bus) or 0
    except KeyboardInterrupt:
        return 0
    finally:
        bus.stop()


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
    if verb in ("eval", "gate"):                 # no agent needed: they build their own
        from pocket.evals import main as run_evals

        return run_evals(["--gate"] if verb == "gate" else argv[1:])
    if verb == "judge":
        from pocket.judge import main as run_judge

        return run_judge()
    settings = load_settings()
    if verb == "demo":
        settings.graph_workflows = True         # show the graph front door too
    if verb in ("team", "tools"):
        settings.team = True                    # so assign_team is visible in both
    pocket = Pocket(settings, confirm=cli_confirm)
    try:
        return {"chat": chat, "demo": demo, "trace": trace, "team": team,
                "tools": tools, "mcp": mcp,
                "dashboard": dashboard, "telegram": telegram}.get(verb, chat)(pocket)
    finally:
        pocket.close()


if __name__ == "__main__":
    raise SystemExit(main())
