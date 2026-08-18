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
"""

from __future__ import annotations

from pocket.loop import run_loop
from pocket.tools import Tool, ToolRegistry

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
        # never the delegate tool itself: one level of delegation, by construction
        allowed = [n for n in (wanted or registry.names()) if n != "delegate"]
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
