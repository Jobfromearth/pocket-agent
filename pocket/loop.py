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

A third thing happens on every pass and is not a guardrail but a budget: `fit`
runs before EVERY model call, not once when the turn starts. A turn that calls
eight tools appends eight results, and checking the window only at the top means
checking it when it was never going to be a problem. `fit` is injected, so the
loop does not need to know how the window is kept — see context.py.

If the provider says the prompt is too long anyway, the loop shortens hard and
retries ONCE. A second refusal is raised: retrying forever turns a bug into a
bill.
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
    tokens_cached: int = 0


TOO_LONG = ("too long", "too many tokens", "context length", "maximum context",
            "context_length_exceeded", "prompt is too long")


def is_too_long(exc: Exception) -> bool:
    """Providers disagree about the wording and agree about nothing else, so the
    check is on the message. A false negative just re-raises, which is what
    would have happened anyway."""
    text = str(exc).lower()
    return any(phrase in text for phrase in TOO_LONG)


def run_loop(client, model: str, system: str | list[str], messages: list[dict],
             tools: ToolRegistry,
             max_iterations: int = 8, max_tokens: int = 4096,
             observer: Observer | None = None, fit=None) -> LoopResult:
    """Run one agent turn. `messages` is mutated in place, so afterwards it holds
    the full working memory of the turn — which is exactly what gets traced.

    `fit(messages) -> (messages, shrunk)` keeps the turn inside its window. It is
    injected because the loop should not own the policy, and it is optional
    because a sub-agent with three tools does not need one."""
    notify = observer or (lambda kind, event: None)
    result = LoopResult(reply="")

    def ask():
        return client.messages.create(model=model, system=system, messages=messages,
                                      tools=tools.schemas(), max_tokens=max_tokens)

    for iteration in range(1, max_iterations + 1):
        result.iterations = iteration

        # ---- reason
        if fit is not None:
            _, shrunk = fit(messages)
            if shrunk:
                notify("fit", {"iteration": iteration, "results_shortened": shrunk})
        try:
            response = ask()
        except Exception as exc:
            if fit is None or not is_too_long(exc):
                raise
            # the window said it fit and the provider disagreed: shorten hard,
            # try once more, and let a second refusal out — it is a real bug
            _, shrunk = fit(messages, 0)
            notify("fit", {"iteration": iteration, "results_shortened": shrunk,
                           "reactive": True, "provider_said": str(exc)[:200]})
            response = ask()
        usage = response.usage
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        written = getattr(usage, "cache_creation_input_tokens", 0) or 0
        result.tokens_in += usage.input_tokens
        result.tokens_out += usage.output_tokens
        result.tokens_cached += cached
        notify("llm", {"iteration": iteration, "model": model,
                       "stop_reason": response.stop_reason,
                       "usage": {"in": usage.input_tokens, "out": usage.output_tokens,
                                 "cached": cached, "cache_written": written}})
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
