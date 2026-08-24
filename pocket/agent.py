"""Wiring — one assistant, built from its parts. Gateways only call respond().

    config -> db -> tools -> memory -> session -> loop
                                          |
                            optional triage graph in front (fails open)

If you want to understand the whole thing in one file, it is this one.
"""

from __future__ import annotations

import time

from pocket.coder import make_coder_tool
from pocket.config import Settings, load_settings
from pocket.context import (
    compact_history,
    fit_for_model,
    make_read_artifact_tool,
    make_read_history_tool,
    offload_if_large,
)
from pocket.db import connect
from pocket.graph import build_triage_graph, classify_message, run_graph, todays_events
from pocket.hooks import Hooks
from pocket.injection import Screen
from pocket.loop import LoopResult, run_loop
from pocket.mcp import connect_servers, load_config
from pocket.memory import Memory, make_memory_tools
from pocket.models import get_client, resolve
from pocket.permissions import Policy
from pocket.session import Session
from pocket.skills import make_read_skill_tool
from pocket.subagent import FanOut, make_delegate_tool
from pocket.team import make_team_tool
from pocket.tools import build_registry
from pocket.trace import Tracer
from pocket.web import make_web_tools


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
        self.hooks = Hooks()
        self.tools = build_registry(
            self.conn, self.settings.home, policy=self.policy, hooks=self.hooks,
            # every tool result passes through offloading on its way to the model
            on_result=lambda name, output: offload_if_large(
                name, output, self.settings.home, self.settings.tool_result_limit))
        self.tools.register(make_read_artifact_tool(self.settings.home))
        self.tools.register(make_read_history_tool(self.conn))
        if self.settings.web:
            for tool in make_web_tools():
                self.tools.register(tool)
        if self.settings.coder:
            self.tools.register(make_coder_tool(self.settings.home))
        if self.settings.subagents:
            self.tools.register(make_delegate_tool(
                self.client, self.settings.model, self.tools,
                max_iterations=max(2, self.settings.max_iterations // 2),
                max_tokens=self.settings.max_tokens))
        self.tools.register(make_read_skill_tool(self.memory.skills))
        if self.settings.self_edit:
            for tool in make_memory_tools(self.conn, self.settings,
                                          self.memory.skills):
                self.tools.register(tool)
        self.session = Session(self.settings, memory=self.memory)
        self.tracer = Tracer(self.settings)
        if self.settings.team:
            # the team's own events (waves, workers, routes) land in the same
            # trace as the turn that started it — one tape, not two
            self.tools.register(make_team_tool(
                self.client, self.settings.model, self.tools, self.conn,
                max_iterations=max(2, self.settings.max_iterations // 2),
                max_tokens=self.settings.max_tokens, observer=self.tracer.event))
        # One turn may start only so many other agents. Registered before the
        # screen so a refused fan-out never spends its budget.
        fanout = FanOut(limit=self.settings.fanout_per_turn)
        self.hooks.add("turn_start", fanout.turn_start)
        self.hooks.add("before_tool", fanout.before_tool)
        # Untrusted output is fenced on the way in, and the call after a high-risk
        # result is escalated to ask-the-human. Registered last so it sees every
        # tool, including the ones MCP is about to add.
        if self.settings.screen_injection:
            screen = Screen(self.tools, notify=self.tracer.event)
            self.hooks.add("after_tool", screen.after_tool)
            self.hooks.add("before_tool", screen.before_tool)
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
        self.hooks.notify = notify
        started = time.perf_counter()
        refused = self.hooks.run("turn_start", user_message)
        if refused is not None:
            return LoopResult(reply=refused, iterations=0)

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
            self.hooks.run("turn_end", user_message, result.reply)
        return result

    def _full_turn(self, user_message: str, notify) -> LoopResult:
        """The classic turn. The graph's full_agent node calls this same method,
        so loop-as-a-node can never drift from loop-as-default."""
        # kept split so the cache breakpoint lands after the stable half; a hook
        # that rewrites the prompt collapses it back to one block, which costs
        # the breakpoint and is the hook's business
        system = self.session.build_system_parts(user_message, notify=notify)
        rewritten = self.hooks.run("system_built", "\n".join(p for p in system if p))
        if rewritten is not None:
            system = rewritten
        messages, folded = compact_history(self.session.messages_for(user_message, notify),
                                           self.settings.context_budget_chars, self.summarise)
        if folded:
            notify("compaction", {"messages_folded": folded})
        return run_loop(client=self.client, model=self.settings.model, system=system,
                        messages=messages, tools=self.tools,
                        max_iterations=self.settings.max_iterations,
                        max_tokens=self.settings.max_tokens, observer=notify,
                        fit=self.fit)

    def fit(self, messages: list[dict], budget: int | None = None):
        """The loop's window policy, as one injected callable. `budget=0` is the
        reactive case: shorten everything shortenable, keep nothing whole."""
        if budget == 0:
            return fit_for_model(messages, 0, keep_whole=0)
        return fit_for_model(messages, budget or self.settings.context_budget_chars)

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
