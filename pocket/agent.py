"""Wiring — one assistant, built from its parts. Gateways only call respond().

    config -> db -> tools -> memory -> session -> loop
                                          |
                            optional triage graph in front (fails open)

If you want to understand the whole thing in one file, it is this one.
"""

from __future__ import annotations

import time

from pocket.config import Settings, load_settings
from pocket.context import compact_history, make_read_artifact_tool, offload_if_large
from pocket.db import connect
from pocket.graph import build_triage_graph, classify_message, run_graph, todays_events
from pocket.loop import LoopResult, run_loop
from pocket.mcp import connect_servers, load_config
from pocket.memory import Memory
from pocket.models import get_client, resolve
from pocket.permissions import Policy
from pocket.session import Session
from pocket.subagent import make_delegate_tool
from pocket.tools import build_registry
from pocket.trace import Tracer


def compose(*observers):
    """Fan one event out to several listeners (gateway, tracer, capture)."""
    listeners = [o for o in observers if o]

    def notify(kind, event):
        for listener in listeners:
            listener(kind, event)

    return notify


class Pocket:
    def __init__(self, settings: Settings | None = None, client=None, conn=None,
                 confirm=None):
        # client, conn and confirm are injectable: evals swap in a scripted model,
        # a temp database and a scripted human through the same seams the app uses.
        self.settings = settings or load_settings()
        self.settings.ensure_home()
        resolve(self.settings)
        self.conn = conn or connect(self.settings.home)
        self.client = client if client is not None else get_client(self.settings)
        self.memory = Memory(self.conn, self.settings, self.client)
        self.policy = Policy(confirm=confirm)
        self.tools = build_registry(
            self.conn, self.settings.home, policy=self.policy,
            # every tool result passes through offloading on its way to the model
            on_result=lambda name, output: offload_if_large(
                name, output, self.settings.home, self.settings.tool_result_limit))
        self.tools.register(make_read_artifact_tool(self.settings.home))
        if self.settings.subagents:
            self.tools.register(make_delegate_tool(
                self.client, self.settings.model, self.tools,
                max_iterations=max(2, self.settings.max_iterations // 2),
                max_tokens=self.settings.max_tokens))
        self.session = Session(self.settings, memory=self.memory)
        self.tracer = Tracer(self.settings)
        # MCP servers are opt-in by the presence of .pocket/mcp.json, and a broken
        # one is reported and skipped — never fatal.
        self.mcp_servers = connect_servers(
            self.tools, load_config(self.settings.home), notify=self.tracer.event)

    def close(self) -> None:
        for server in self.mcp_servers:
            server.close()

    def summarise(self, prompt: str) -> str:
        """One cheap-model call that returns plain text — used by compaction."""
        response = self.client.messages.create(
            model=self.settings.small_model, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in response.content if b.type == "text")

    def respond(self, user_message: str, observer=None) -> LoopResult:
        """One full turn: assemble working memory, run the loop, persist, trace."""
        captured: dict = {}

        def capture(kind, event):
            if kind == "gate":
                captured["gate"] = {"decision": event.get("decision"),
                                    "reason": event.get("reason")}
            elif kind == "route":
                captured["route"] = event.get("target")
            elif kind == "graph_end":
                captured["graph_path"] = event.get("path")

        notify = compose(observer, self.tracer.event, capture)
        started = time.perf_counter()

        with self.tracer.turn(user_message):
            result = None
            if self.settings.graph_workflows:
                # The graph front door can never make things worse: any failure
                # anywhere below falls through to the plain loop.
                try:
                    result = self._respond_via_graph(user_message, notify)
                except Exception as exc:
                    notify("graph_end", {"workflow": "triage", "path": [], "error": repr(exc)})
            if result is None:
                result = self._full_turn(user_message, notify)

            quick = captured.get("route") == "quick_reply"
            meta = {
                "gate": captured.get("gate"),
                "route": "quick" if quick else ("full" if "route" in captured else None),
                "graph_path": captured.get("graph_path"),
                "iterations": result.iterations,
                "tools": [c["tool"] for c in result.tool_calls],
                "latency_ms": int((time.perf_counter() - started) * 1000),
                # which brain answered — a quick graph turn used the small model;
                # say so rather than reporting the model the user configured
                "model": self.settings.small_model if quick else self.settings.model,
            }
            self.session.add_exchange(user_message, result.reply,
                                      tool_calls=result.tool_calls, meta=meta)
            self.memory.maybe_consolidate(notify=notify)
            self.memory.export_markdown()
            self.tracer.end_turn(result.reply, meta)
        return result

    def _full_turn(self, user_message: str, notify) -> LoopResult:
        """The classic turn. The graph's full_agent node calls this same method,
        so loop-as-a-node can never drift from loop-as-default."""
        system = self.session.build_system(user_message, notify=notify)
        messages, folded = compact_history(self.session.messages_for(user_message),
                                           self.settings.context_budget_chars, self.summarise)
        if folded:
            notify("compaction", {"messages_folded": folded})
        return run_loop(client=self.client, model=self.settings.model, system=system,
                        messages=messages, tools=self.tools,
                        max_iterations=self.settings.max_iterations,
                        max_tokens=self.settings.max_tokens, observer=notify)

    def _respond_via_graph(self, user_message: str, notify) -> LoopResult | None:
        """One turn through the triage graph. Returns None whenever the graph did
        not produce an answer, and respond() then falls open to the plain loop."""
        def quick_fn(state: dict) -> str:
            from pocket.graph import QUICK_PROMPT

            response = self.client.messages.create(
                model=self.settings.small_model, max_tokens=400,
                messages=[{"role": "user", "content": QUICK_PROMPT.format(
                    calendar=state.get("calendar", ""), message=state["message"])}])
            return "".join(b.text for b in response.content if b.type == "text")

        graph = build_triage_graph(
            classify_fn=lambda m: classify_message(self.client, self.settings.small_model, m),
            calendar_fn=lambda: todays_events(self.settings.home),
            quick_fn=quick_fn,
            full_fn=lambda state: self._full_turn(state["message"], notify))
        state = run_graph(graph, {"message": user_message}, observer=notify)
        if state.get("errors"):
            return None
        if isinstance(state.get("result"), LoopResult):
            return state["result"]
        if state.get("reply"):
            return LoopResult(reply=state["reply"], iterations=1)
        return None
