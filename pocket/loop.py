"""THE LOOP — reason, act, observe, repeat. Everything else is scaffolding.

    while not done:
        response = llm(messages, tools)      # reason
        if response asks for tools:
            results = run(tool_calls)        # act
            messages += results              # observe
        else:
            done                             # reply to the human

Two guardrails end every turn, and they are the reason a loop is safe to ship:
  1. the model stops asking for tools -> natural end
  2. max_iterations is reached        -> hard stop, it can never spin forever
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pocket.tools import ToolRegistry

LoopEvent = dict[str, Any]
Observer = Callable[[str, LoopEvent], None]   # notify(kind, event): gateways + tracing


@dataclass
class LoopResult:
    reply: str
    tool_calls: list[LoopEvent] = field(default_factory=list)
    iterations: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


def run_loop(client, model: str, system: str, messages: list[dict], tools: ToolRegistry,
             max_iterations: int = 8, max_tokens: int = 4096,
             observer: Observer | None = None) -> LoopResult:
    """Run one agent turn. `messages` is mutated in place, so afterwards it holds
    the full working memory of the turn — which is exactly what gets traced."""
    notify = observer or (lambda kind, event: None)
    result = LoopResult(reply="")

    for iteration in range(1, max_iterations + 1):
        result.iterations = iteration

        # ---- reason
        response = client.messages.create(model=model, system=system, messages=messages,
                                          tools=tools.schemas(), max_tokens=max_tokens)
        result.tokens_in += response.usage.input_tokens
        result.tokens_out += response.usage.output_tokens
        notify("llm", {"iteration": iteration, "model": model,
                       "stop_reason": response.stop_reason,
                       "usage": {"in": response.usage.input_tokens,
                                 "out": response.usage.output_tokens}})
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:                     # guardrail 1: talking to the human
            result.reply = "".join(b.text for b in response.content if b.type == "text")
            return result

        # ---- act, then observe: results go back in as the next user turn
        tool_results = []
        for call in tool_uses:
            output = tools.execute(call.name, call.input)
            event = {"tool": call.name, "args": call.input, "output": output}
            result.tool_calls.append(event)
            notify("tool", event)
            tool_results.append({"type": "tool_result", "tool_use_id": call.id, "content": output})
        messages.append({"role": "user", "content": tool_results})

    # guardrail 2: out of iterations. Say so honestly instead of pretending.
    result.reply = ("(I hit my iteration limit before finishing — "
                    "try breaking the request into smaller steps.)")
    return result
