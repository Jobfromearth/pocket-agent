"""Graphs — structure AROUND the loop, never instead of it.

A loop DISCOVERS what to do next; a graph PRE-DETERMINES what happens next.
Both ship here. The engine runs a graph in waves:

    while some nodes are ready:
        run every ready node (in parallel, if several are ready)
        merge what they wrote into one shared state dict
        fire their edges, or ask their router where to go

Three ideas carry it: state is a plain dict (parallel nodes must write disjoint
keys); routers are plain Python over that state, so a model never decides
control flow directly; and guards are the loop's two guardrails generalised —
per-node max_visits plus a global max_steps.

The shipped workflow is triage. It is the retrieval gate's idea widened from
one gate to a shape: classify the message with a small model WHILE the calendar
loads, then route. "thanks!" never wakes the big model; a real task runs the
exact same loop as always, as one node. Every seam fails open to the plain loop,
so turning this on can only ever save time — never remove capability.
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

START, END = "START", "END"


class GraphStateCollision(Exception):
    """Two nodes in one wave wrote the same key — a graph bug, not a race."""


@dataclass
class Node:
    name: str
    fn: Callable[[dict], dict]
    kind: str = "fn"            # fn | tool | llm | agent, for describe()
    max_visits: int = 1


@dataclass
class Graph:
    name: str
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)
    routers: dict[str, tuple[Callable[[dict], str], dict[str, str]]] = field(default_factory=dict)

    def add_node(self, node: Node) -> None:
        self.nodes[node.name] = node

    def add_edge(self, src: str, dst: str) -> None:
        for end in (src, dst):
            if end not in self.nodes and end not in (START, END):
                raise ValueError(f"unknown node '{end}'")
        self.edges.append((src, dst))

    def add_router(self, src: str, route: Callable[[dict], str], targets: dict[str, str]) -> None:
        self.routers[src] = (route, targets)

    def describe(self) -> dict:
        """The topology as data. Draw the picture from THIS, never by hand, and
        the diagram cannot drift from the code."""
        edges = [{"src": s, "dst": d, "conditional": False} for s, d in self.edges]
        for src, (_route, targets) in self.routers.items():
            edges += [{"src": src, "dst": d, "conditional": True}
                      for d in dict.fromkeys(targets.values())]
        return {"name": self.name,
                "nodes": [{"name": n.name, "kind": n.kind} for n in self.nodes.values()],
                "edges": edges}


def run_graph(graph: Graph, state: dict, observer=None, max_steps: int = 20) -> dict:
    """Run one workflow to completion. Node errors land in state['errors'];
    they are never raised out of the run, same rule as ToolRegistry.execute."""
    lock = threading.Lock()
    raw = observer or (lambda kind, event: None)

    def notify(kind, event):
        with lock:                       # pool threads share the trace file
            raw(kind, event)

    deps: dict[str, set] = defaultdict(set)
    for src, dst in graph.edges:
        if dst != END:
            deps[dst].add(src)
    fired: dict[str, set] = defaultdict(set)
    runs: dict[str, int] = defaultdict(int)
    errors = state.setdefault("errors", {})
    path: list[str] = []
    started = time.perf_counter()
    notify("graph_start", {"workflow": graph.name, "nodes": list(graph.nodes)})

    def ready(jumps: list[str]) -> list[str]:
        wave = []
        for name in jumps + [n for n in graph.nodes
                             if deps[n] and deps[n] <= fired[n] and runs[n] == 0]:
            if name == END or name in wave or runs[name] >= graph.nodes[name].max_visits:
                continue
            wave.append(name)
        return wave

    def run_one(name: str):
        snapshot = dict(state)
        snapshot["_notify"] = lambda kind, ev, _n=name: notify(kind, {**ev, "node": _n})
        clock = time.perf_counter()
        try:
            return name, graph.nodes[name].fn(snapshot) or {}, None, int((time.perf_counter() - clock) * 1000)
        except Exception as exc:
            return name, None, repr(exc), int((time.perf_counter() - clock) * 1000)

    for src, dst in graph.edges:
        if src == START:
            fired[dst].add(START)
    wave = ready([])

    while wave:
        if len(path) + len(wave) > max_steps:
            errors.setdefault("engine", f"max_steps={max_steps} reached")
            break
        for name in wave:
            runs[name] += 1
            notify("node_start", {"workflow": graph.name, "node": name})
        if len(wave) == 1:               # solo node runs inline: trace order stays exact
            results = [run_one(wave[0])]
        else:
            with ThreadPoolExecutor(max_workers=len(wave)) as pool:
                results = [f.result() for f in [pool.submit(run_one, n) for n in wave]]

        jumps: list[str] = []
        written: dict[str, str] = {}
        for name, out, error, ms in results:
            path.append(name)
            for key in [k for k in (out or {}) if not k.startswith("_")]:
                if written.get(key, name) != name:
                    raise GraphStateCollision(
                        f"'{name}' and '{written[key]}' both wrote '{key}'")
                written[key] = name
                state[key] = out[key]
            notify("node_end", {"workflow": graph.name, "node": name, "ms": ms, "error": error})
            if error:
                errors[name] = error
                continue                 # no edges fire: the run drains to END
            if name in graph.routers:
                route, targets = graph.routers[name]
                label = route(state)
                target = targets.get(label)
                notify("route", {"workflow": graph.name, "router": name,
                                 "target": target or END, "reason": label})
                if target and target != END:
                    jumps.append(target)
            else:
                for src, dst in graph.edges:
                    if src == name and dst != END:
                        fired[dst].add(name)
        wave = ready(jumps)

    notify("graph_end", {"workflow": graph.name, "path": path,
                         "ms": int((time.perf_counter() - started) * 1000),
                         "error": next(iter(errors.values()), None)})
    return state


# ------------------------------------------------------------------ triage
TRIAGE_PROMPT = """\
You are a triage gate for a personal assistant. Decide which brain answers.

Reply with ONLY this JSON, nothing else:
{{"route": "quick" or "full", "reason": "<5 words>"}}

quick - greetings, thanks, acknowledgements: nothing to do, nothing to look up.
full  - anything mentioning tasks, schedules, people, notes, memory, or needing
        a tool. When unsure, choose full.

User message: {message}"""

QUICK_PROMPT = """\
You are pocket, a warm and concise assistant. This message needs no tools and
no memory — reply in one or two short sentences.

Today's calendar: {calendar}
User message: {message}"""


def classify_message(client, small_model: str, message: str) -> tuple[str, str]:
    """(route, reason). Fails open to 'full' — a broken classifier must cost a
    little latency, never capability."""
    try:
        response = client.messages.create(
            model=small_model, max_tokens=600,
            messages=[{"role": "user", "content": TRIAGE_PROMPT.format(message=message)}])
        text = "".join(b.text for b in response.content if b.type == "text")
        if "{" not in text:
            return "full", "no JSON - failing open"
        decision = json.loads(text[text.index("{"): text.rindex("}") + 1])
        route = decision.get("route")
        if route not in ("quick", "full"):
            return "full", f"bad route {route!r} - failing open"
        return route, decision.get("reason", "")
    except Exception as exc:
        return "full", f"triage failed open ({type(exc).__name__})"


def todays_events(home: Path) -> str:
    """Read today's events straight off calendar.ics — a local file read, which
    is exactly why it can run in parallel with the classifier's network call."""
    ics = home / "calendar.ics"
    if not ics.exists():
        return "(no calendar)"
    today = datetime.now().strftime("%Y%m%d")
    titles, title = [], ""
    for line in ics.read_text(encoding="utf-8").splitlines():
        if line.startswith("SUMMARY:"):
            title = line[len("SUMMARY:"):]
        elif line.startswith("DTSTART") and today in line and title:
            titles.append(title)
    return "; ".join(titles) or "(nothing today)"


def build_triage_graph(*, classify_fn, calendar_fn, quick_fn, full_fn) -> Graph:
    """Callables are injected so evals can script them and a viewer can build
    the graph with stubs just to describe() its shape."""
    graph = Graph("triage")

    def classify(state: dict) -> dict:
        route, reason = classify_fn(state["message"])
        if state.get("_notify"):
            state["_notify"]("triage", {"route": route, "reason": reason})
        return {"route": route, "triage_reason": reason}

    graph.add_node(Node("classify", classify, kind="llm"))
    graph.add_node(Node("check_calendar", lambda s: {"calendar": calendar_fn()}, kind="tool"))
    graph.add_node(Node("gather", lambda s: {}, kind="fn"))
    graph.add_node(Node("quick_reply", lambda s: {"reply": quick_fn(s)}, kind="llm"))
    graph.add_node(Node("full_agent", lambda s: {"result": full_fn(s)}, kind="agent"))
    graph.add_edge(START, "classify")
    graph.add_edge(START, "check_calendar")
    graph.add_edge("classify", "gather")          # the router waits on BOTH branches
    graph.add_edge("check_calendar", "gather")
    graph.add_router("gather", lambda s: s.get("route") or "full",
                     {"quick": "quick_reply", "full": "full_agent"})
    graph.add_edge("quick_reply", END)
    graph.add_edge("full_agent", END)
    return graph
