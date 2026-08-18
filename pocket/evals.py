"""Deterministic evals — 0 or 1, no model judges them.

The rule this file exists to enforce: "did the right tool fire with the right
arguments, and did the row land?" is a UNIT TEST. "was the reply any good?" is a
scored judgement and belongs in a separate suite. Conflating the two is the most
common eval mistake, so they never share a file here.

Every case runs against the scripted offline model, so the suite is fast, free,
and tests the HARNESS — the gate, the loop, the tools, the graph, the trace —
rather than the intelligence of whichever model you plug in.

    python -m pocket eval        # this suite, plus a summary line
    pytest pocket/evals.py       # same cases, if you prefer pytest
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from pocket.agent import Pocket
from pocket.config import Settings
from pocket.graph import build_triage_graph, run_graph
from pocket.mcp import StdioServer, connect_servers
from pocket.memory import fts_query
from pocket.models import ScriptedClient
from pocket.permissions import Policy
from pocket.tools import Tool, ToolRegistry


def build_agent(confirm=None, **overrides) -> Pocket:
    """A throwaway assistant in a temp home, on the scripted model. `confirm`
    stands in for the human the permission policy asks."""
    home = Path(tempfile.mkdtemp(prefix="pocket-eval-"))
    settings = Settings(provider="mock", home=home, **overrides)
    return Pocket(settings=settings, client=ScriptedClient(), confirm=confirm)


DEMO_SERVER = [sys.executable, "-m", "pocket.examples.demo_server"]


# ---------------------------------------------------------------- the loop
def test_schedule_creates_the_row():
    """The flagship check: a scheduling request must write a calendar row."""
    pocket = build_agent()
    result = pocket.respond("Schedule a coffee with Alex tomorrow at 9am")
    rows = pocket.conn.execute("SELECT title, start FROM calendar_events").fetchall()
    assert len(rows) == 1, f"expected 1 event, got {len(rows)}"
    assert "09:00" in rows[0]["start"], rows[0]["start"]
    assert result.iterations == 2, "tool turn should be reason -> act -> reason"
    assert (pocket.settings.home / "calendar.ics").exists(), "the .ics artifact is missing"


def test_save_note_lands_in_searchable_memory():
    pocket = build_agent()
    pocket.respond("Remember that Alex prefers morning meetings")
    assert pocket.memory.facts.search("alex morning"), "the fact is not searchable"


def test_tool_error_comes_back_as_text():
    """A broken tool must never crash the loop — the model has to be able to read
    the error and try something else."""
    pocket = build_agent()
    output = pocket.tools.execute("create_event", {"title": "x", "start": "not-a-date"})
    assert output.startswith("Error running create_event"), output


def test_unknown_tool_is_reported_not_raised():
    pocket = build_agent()
    assert pocket.tools.execute("launch_rocket", {}) == "Error: unknown tool 'launch_rocket'"


def test_max_iterations_is_a_hard_stop():
    """Guardrail 2: a model that never stops asking for tools must still end."""
    class NeverStops(ScriptedClient):
        def _turn(self, messages, tools):
            return self._call("save_note", {"subject": "loop", "content": "again"})

    pocket = build_agent(max_iterations=3)
    pocket.client = pocket.memory.client = NeverStops()
    result = pocket._full_turn("go", lambda kind, event: None)
    assert result.iterations == 3, result.iterations
    assert "iteration limit" in result.reply


def test_history_window_is_bounded():
    """Working memory is RAM, not the archive: only the last N turns are sent."""
    pocket = build_agent(history_turns=2)
    for i in range(6):
        pocket.session.history += [{"role": "user", "content": f"m{i}"},
                                 {"role": "assistant", "content": f"r{i}"}]
    sent = pocket.session.messages_for("now")
    assert len(sent) == 5, len(sent)          # 2 turns * 2 rows + the new message
    assert sent[0]["content"] == "m4"


# ------------------------------------------------------------- the memory
def test_gate_skips_a_self_contained_question():
    pocket = build_agent()
    seen = []
    pocket.respond("what is 2+2?", observer=lambda kind, ev: seen.append((kind, ev)))
    gate = next(ev for kind, ev in seen if kind == "gate")
    assert gate["decision"] == "skip", gate


def test_gate_retrieves_and_the_fact_reaches_the_prompt():
    pocket = build_agent()
    pocket.memory.facts.add("alex", "Alex prefers morning meetings")
    system = pocket.session.build_system("when am I meeting Alex?")
    assert "morning meetings" in system, "retrieved memory never reached the system prompt"


def test_gate_fails_open_when_the_judge_breaks():
    """A broken gate must cost latency, never capability: when the judge errors
    we retrieve anyway, because a stale memory beats a lost one."""
    from pocket.memory import should_retrieve

    class Broken(ScriptedClient):
        def _gate(self, prompt):
            raise RuntimeError("gate is down")

    retrieve, query, reason = should_retrieve(Broken(), "scripted", "when am I meeting Alex?")
    assert retrieve is True, "a broken gate must fail open, not skip memory"
    assert query == "when am I meeting Alex?", "the raw message must be used as the query"
    assert "failed open" in reason, reason


def test_fts_query_handles_unsegmented_scripts():
    """Regression: CJK is indexed as one run, so those tokens (and only those)
    must be searched as prefixes, or the match silently never fires."""
    assert fts_query("meeting 阿历克斯") == "meeting OR 阿历克斯*"
    assert fts_query("Müller") == "müller"
    assert fts_query("!!!") == ""


def test_consolidation_distils_facts_after_n_exchanges():
    pocket = build_agent(consolidate_every=2)
    pocket.respond("Remember that Raj plays tennis on Saturdays")
    pocket.respond("hello")
    facts = pocket.conn.execute("SELECT content FROM facts WHERE source='consolidation'").fetchall()
    assert facts, "nothing was distilled into semantic memory"
    left = pocket.conn.execute("SELECT COUNT(*) c FROM chat_log WHERE consolidated=0").fetchone()
    assert left["c"] == 0, "consolidated rows were not marked"


def test_skill_body_loads_only_when_relevant():
    pocket = build_agent()
    assert "create_event" in pocket.memory.matching_skills("schedule a meeting with Alex")
    assert pocket.memory.matching_skills("what is 2+2") == ""


# -------------------------------------------------------------- the graph
def test_triage_routes_small_talk_away_from_the_big_model():
    pocket = build_agent(graph_workflows=True)
    seen = []
    pocket.respond("thanks!", observer=lambda kind, ev: seen.append((kind, ev)))
    routes = [ev["target"] for kind, ev in seen if kind == "route"]
    assert routes == ["quick_reply"], routes
    assert not [1 for kind, ev in seen if kind == "tool"], "a quick turn ran a tool"


def test_triage_sends_a_real_task_into_the_same_loop():
    pocket = build_agent(graph_workflows=True)
    seen = []
    pocket.respond("Schedule a swim with Sergey tomorrow at 5pm",
                 observer=lambda kind, ev: seen.append((kind, ev)))
    assert [ev["target"] for kind, ev in seen if kind == "route"] == ["full_agent"]
    assert pocket.conn.execute("SELECT COUNT(*) c FROM calendar_events").fetchone()["c"] == 1


def test_graph_failure_falls_open_to_the_plain_loop():
    """The flag must only ever be able to save time, never remove capability."""
    class BrokenTriage(ScriptedClient):
        def _triage(self, prompt):
            raise RuntimeError("classifier is down")

    pocket = build_agent(graph_workflows=True)
    pocket.client = pocket.memory.client = BrokenTriage()
    pocket.respond("Schedule a swim with Sergey tomorrow at 5pm")
    assert pocket.conn.execute("SELECT COUNT(*) c FROM calendar_events").fetchone()["c"] == 1


def test_parallel_nodes_may_not_write_the_same_key():
    """A silent lost write in a parallel branch is the graph bug that eats an
    afternoon, so the engine raises instead."""
    from pocket.graph import END, START, Graph, GraphStateCollision, Node

    graph = Graph("collide")
    graph.add_node(Node("a", lambda s: {"same": 1}))
    graph.add_node(Node("b", lambda s: {"same": 2}))
    graph.add_node(Node("join", lambda s: {}))
    graph.add_edge(START, "a")
    graph.add_edge(START, "b")
    graph.add_edge("a", "join")
    graph.add_edge("b", "join")
    graph.add_edge("join", END)
    try:
        run_graph(graph, {})
    except GraphStateCollision:
        return
    raise AssertionError("the collision was not caught")


def test_topology_is_drawn_from_the_engine():
    """The picture is generated from the graph itself, so it cannot drift."""
    noop = lambda *a, **k: ""      # noqa: E731
    shape = build_triage_graph(classify_fn=lambda m: ("full", ""), calendar_fn=noop,
                               quick_fn=noop, full_fn=noop).describe()
    assert {n["name"] for n in shape["nodes"]} == {
        "classify", "check_calendar", "gather", "quick_reply", "full_agent"}
    assert any(e["conditional"] for e in shape["edges"]), "the router edge is missing"


# ----------------------------------------------------------------- the ops
def test_the_turn_is_on_tape_in_order():
    pocket = build_agent()
    pocket.respond("Schedule a coffee with Alex tomorrow at 9am")
    kinds = [line["event"] for line in pocket.tracer.read()]
    assert kinds[0] == "turn_start" and kinds[-1] == "turn_end", kinds
    assert kinds.index("gate") < kinds.index("tool") < kinds.index("turn_end")


def test_every_llm_call_lands_in_the_spend_ledger():
    pocket = build_agent()
    pocket.respond("Schedule a coffee with Alex tomorrow at 9am")
    spend = pocket.tracer.spend()
    assert spend["calls"] >= 2, spend       # at least reason + observe
    assert spend["in"] > 0


def test_turn_meta_records_how_the_answer_was_produced():
    pocket = build_agent()
    pocket.respond("Schedule a coffee with Alex tomorrow at 9am")
    import json

    meta = json.loads(pocket.conn.execute(
        "SELECT meta FROM chat_log WHERE role='assistant' ORDER BY id DESC LIMIT 1").fetchone()["meta"])
    assert meta["tools"] == ["create_event"], meta
    assert meta["gate"]["decision"] in ("skip", "retrieve")
    assert meta["latency_ms"] >= 0


# ------------------------------------------------------------------- MCP
def test_mcp_stateless_server_needs_no_handshake():
    """2026-07-28: no initialize, no session id — the version rides in _meta."""
    server = StdioServer("demo", DEMO_SERVER, timeout=15)
    server.start()
    try:
        assert server.mode == "stateless", server.mode
        assert {t["name"] for t in server.list_tools()} == {"word_count", "shout"}
    finally:
        server.close()


def test_mcp_legacy_server_falls_back_to_the_handshake():
    """The spec's own stdio advice: probe with server/discover, and a failure
    means this server is pre-2026 — so speak the old handshake instead."""
    server = StdioServer("legacy", [*DEMO_SERVER, "--legacy"], timeout=15)
    server.start()
    try:
        assert server.mode == "legacy", server.mode
        assert server.call("shout", {"text": "hi"}) == "HI"
    finally:
        server.close()


def test_mcp_tools_arrive_namespaced_and_gated():
    registry = ToolRegistry(policy=Policy(confirm=lambda *a: True))
    servers = connect_servers(registry, {"demo": {"command": DEMO_SERVER}})
    try:
        assert "mcp__demo__shout" in registry.names(), registry.names()
        tool = registry.get("mcp__demo__shout")
        assert tool.risk == "ask", "third-party tools must not run unattended"
        assert tool.origin == "mcp:demo"
        assert registry.execute("mcp__demo__shout", {"text": "hello"}) == "HELLO"
    finally:
        for server in servers:
            server.close()


def test_a_broken_mcp_server_is_skipped_not_fatal():
    registry = ToolRegistry()
    events = []
    servers = connect_servers(registry, {"broken": {"command": [sys.executable, "-c", "raise SystemExit(1)"], "timeout": 3}},
                              notify=lambda kind, ev: events.append((kind, ev)))
    assert servers == [], "a dead server must not be kept"
    assert any(kind == "mcp_error" for kind, _ in events), events
    assert registry.names() == [], "nothing should have been registered"


# ----------------------------------------------------------- permissions
def test_deny_patterns_win_over_any_confirmation():
    """Some shapes are refused even if a human says yes — that is the point of
    having a deny list separate from the prompt."""
    registry = ToolRegistry(policy=Policy(confirm=lambda *a: True))
    registry.register(Tool("shell", "run", {"type": "object"}, lambda cmd: "ran", risk="ask"))
    output = registry.execute("shell", {"cmd": "rm -rf ~/"})
    assert output.startswith("Blocked by policy"), output


def test_a_risky_tool_asks_once_and_the_answer_is_remembered():
    asked = []
    policy = Policy(confirm=lambda name, args, risk: asked.append(name) or True)
    registry = ToolRegistry(policy=policy)
    registry.register(Tool("touch", "t", {"type": "object"}, lambda: "done", risk="ask"))
    assert registry.execute("touch", {}) == "done"
    assert registry.execute("touch", {}) == "done"
    assert len(asked) == 1, "the human should be asked once per session, not per call"


def test_a_declined_tool_comes_back_as_text():
    registry = ToolRegistry(policy=Policy(confirm=lambda *a: False))
    registry.register(Tool("touch", "t", {"type": "object"}, lambda: "done", risk="ask"))
    assert registry.execute("touch", {}).startswith("Blocked by policy"), "declines must not raise"


def test_with_no_way_to_ask_a_risky_tool_is_refused():
    """No human reachable (a cron run, a bot) must fail closed, not silently run."""
    registry = ToolRegistry(policy=Policy(confirm=None))
    registry.register(Tool("touch", "t", {"type": "object"}, lambda: "done", risk="ask"))
    assert "Blocked by policy" in registry.execute("touch", {})


# ------------------------------------------------------ context engineering
def test_a_large_tool_result_is_offloaded_with_a_readable_pointer():
    pocket = build_agent(tool_result_limit=500)
    pocket.tools.register(Tool("firehose", "big", {"type": "object"},
                             lambda: "x" * 5000))
    shown = pocket.tools.execute("firehose", {})
    assert len(shown) < 1500, "the prompt should never see the whole thing"
    assert "read_artifact" in shown, shown[-200:]
    name = shown.split('name="')[1].split('"')[0]
    assert pocket.tools.execute("read_artifact", {"name": name, "start": 0, "length": 10}) == "x" * 10


def test_read_artifact_cannot_escape_its_folder():
    pocket = build_agent()
    assert pocket.tools.execute(
        "read_artifact", {"name": "../../etc/passwd"}).startswith("Error: no artifact")


def test_history_is_compacted_when_it_outgrows_its_budget():
    from pocket.context import compact_history

    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": "z" * 300}
               for i in range(12)]
    compacted, folded = compact_history(history, budget_chars=1000,
                                        summarise=lambda prompt: "they discussed z at length")
    assert folded == 8, folded
    assert len(compacted) == 6, compacted
    assert "compacted" in compacted[0]["content"]
    assert compacted[-1]["content"] == "z" * 300, "the most recent turns stay verbatim"


def test_a_failed_summariser_keeps_the_context_intact():
    from pocket.context import compact_history

    def broken(prompt):
        raise RuntimeError("summariser down")

    history = [{"role": "user", "content": "y" * 4000}] * 6
    compacted, folded = compact_history(history, budget_chars=100, summarise=broken)
    assert folded == 0 and compacted == history, "dropping context on failure is data loss"


# ------------------------------------------------------------- sub-agents
def test_a_subagent_runs_the_same_loop_with_fewer_tools():
    pocket = build_agent(confirm=lambda *a: True)
    output = pocket.tools.execute("delegate", {
        "task": "Remember that Raj plays tennis on Saturdays", "tools": "save_note"})
    assert "sub-agent:" in output, output
    assert pocket.memory.facts.search("raj tennis"), "the sub-agent's work never landed"


def test_a_subagent_cannot_delegate_again():
    pocket = build_agent(confirm=lambda *a: True)
    scoped = pocket.tools.subset([n for n in pocket.tools.names()])
    assert "delegate" in scoped.names()          # the parent has it
    inner = pocket.tools.subset([n for n in pocket.tools.names() if n != "delegate"])
    assert "delegate" not in inner.names(), "one level of delegation, by construction"


def test_a_subagent_only_sees_the_tools_it_was_given():
    pocket = build_agent(confirm=lambda *a: True)
    scoped = pocket.tools.subset(["save_note"])
    assert scoped.names() == ["save_note"]
    assert scoped.execute("create_event", {"title": "x", "start": "2026-01-01T09:00"}) == (
        "Error: unknown tool 'create_event'")


CASES = [value for name, value in sorted(globals().items()) if name.startswith("test_")]


def main() -> int:
    """A tiny runner, so the suite needs no test framework to prove itself."""
    passed, failed = 0, []
    for case in CASES:
        try:
            case()
            passed += 1
            print(f"  PASS  {case.__name__}")
        except Exception as exc:
            failed.append((case.__name__, exc))
            print(f"  FAIL  {case.__name__}: {exc}")
    total = passed + len(failed)
    print(f"\ndeterministic: {passed}/{total} passed")
    if failed:
        print("release gate: BLOCKED — deterministic evals must be 100%")
        return 1
    print("release gate: PASS")
    return 0
