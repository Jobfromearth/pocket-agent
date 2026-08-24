"""Sub-agents — delegation without handing over control.

A sub-agent is one more `run_loop`, given a narrower brief and a SMALLER tool
registry. Three rules keep it from becoming a swarm you cannot reason about:

  1. the parent never transfers control — the sub-agent runs inside one tool
     call and returns a string, like every other tool;
  2. only the RESULT crosses back, not the sub-agent's transcript, so the
     parent's context stays clean (that is the actual reason to delegate);
  3. the sub-agent cannot delegate again, and cannot reach a tool the parent
     did not name.

That makes the blast radius exactly the tool list you passed in.

What it does NOT bound is how many times a turn may do this. `delegate` asks a
human, and a session grant means it asks once — after which the model can fan
out on every iteration of the loop, each one a whole sub-loop with its own token
bill. Nobody counts that until it shows up on a statement, so `FanOut` counts it
instead.
"""

from __future__ import annotations

from pocket.loop import run_loop
from pocket.tools import Tool, ToolRegistry

# Everything that starts another loop. A team is one entry here and up to eight
# workers underneath, which is exactly why it is capped alongside the others.
FANOUT_TOOLS = ("delegate", "delegate_task", "assign_team")


class FanOut:
    """A per-turn budget for starting other agents, registered as a hook pair.

    The refusal is returned as text, like every other refusal here, so the model
    reads "you have already fanned out twice this turn" and finishes the work
    itself instead of the process dying or the bill continuing."""

    def __init__(self, limit: int = 3):
        self.limit = limit
        self.spent = 0

    def turn_start(self, message: str) -> None:
        self.spent = 0            # a budget that never resets is a hard limit

    def before_tool(self, name: str, args: dict) -> str | None:
        if name not in FANOUT_TOOLS:
            return None
        if self.spent >= self.limit:
            return (f"Refused: this turn has already started {self.spent} sub-agent(s), and "
                    f"the limit is {self.limit}. Do the rest yourself, or answer with what "
                    f"you have and let the user ask for more.")
        self.spent += 1
        return None

SUBAGENT_SYSTEM = """\
You are a focused sub-agent. You were given ONE task and a small set of tools.
Do the task, then reply with the result and nothing else — no preamble, no
questions. If you cannot do it with the tools you have, say exactly why.

Task context from the main assistant:
{context}"""


def make_delegate_tool(client, model: str, registry: ToolRegistry, *,
                       max_iterations: int = 4, max_tokens: int = 2048, observer=None) -> Tool:
    def delegate(task: str, tools: str = "", context: str = "") -> str:
        wanted = [name.strip() for name in tools.split(",") if name.strip()]
        # never anything that fans out again: `tools` is optional, so without this
        # a bare delegate(task=...) handed the sub-agent the parent's whole
        # registry — the coder subprocess and an eight-worker team included
        allowed = [n for n in (wanted or registry.names()) if n not in FANOUT_TOOLS]
        scoped = registry.subset(allowed)
        result = run_loop(client=client, model=model,
                          system=SUBAGENT_SYSTEM.format(context=context or "(none)"),
                          messages=[{"role": "user", "content": task}], tools=scoped,
                          max_iterations=max_iterations, max_tokens=max_tokens,
                          observer=observer)
        used = ", ".join(call["tool"] for call in result.tool_calls) or "none"
        return f"{result.reply}\n[sub-agent: {result.iterations} iterations, tools used: {used}]"

    return Tool(
        name="delegate",
        description=("Hand one self-contained sub-task to a focused sub-agent with a small "
                     "tool set. Use it when a step would otherwise flood your own context. "
                     "Pass `tools` as a comma-separated allow-list."),
        input_schema={"type": "object", "properties": {
            "task": {"type": "string", "description": "the complete instruction, standalone"},
            "tools": {"type": "string", "description": "comma-separated tool names it may use"},
            "context": {"type": "string", "description": "anything it needs to know"}},
            "required": ["task"]},
        fn=delegate,
        risk="ask",                 # it spends tokens and can act: a human sees it first
        origin="subagent")
