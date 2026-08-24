"""Providers — one loop, two wire formats, plus an offline stub.

The loop is written against the Anthropic Messages shape (content blocks,
`tool_use`, `tool_result`). Everything else is an adapter into that shape:

    anthropic-kind   the official SDK, used as-is
    openai-kind      OpenAICompatClient — the ~60 lines that separate the two
                     wire formats (openai / deepseek / kimi / any gateway)
    mock             ScriptedClient — a rule-based stub, NOT a model. It exists
                     so demos and the eval suite run with no key and no network;
                     what it proves is that the harness works, never that a
                     model is smart.

Adding a provider is one row in PROVIDERS.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import ClassVar

from pocket.config import Settings


@dataclass(frozen=True)
class Provider:
    kind: str            # 'anthropic' | 'openai' | 'mock'
    key_env: str
    base_url: str | None
    model: str           # the brain that runs the loop
    small_model: str     # the cheap one: gate, triage, consolidation


# Rate limits are the normal case, not the exception: a free Moonshot tier allows
# three requests a minute, and an eval run makes fifty. Both SDKs honour a
# `retry-after` header, so the only thing missing was the patience to use it.
MAX_RETRIES = 10

PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider("anthropic", "ANTHROPIC_API_KEY", None,
                          "claude-sonnet-5", "claude-haiku-4-5"),
    "openai":    Provider("openai", "OPENAI_API_KEY", None,
                          "gpt-5.5", "gpt-4.1-mini"),
    "deepseek":  Provider("openai", "DEEPSEEK_API_KEY", "https://api.deepseek.com",
                          "deepseek-v4-pro", "deepseek-v4-pro"),
    "kimi":      Provider("anthropic", "MOONSHOT_API_KEY", "https://api.moonshot.ai/anthropic",
                          "kimi-k3", "kimi-k2.6"),
    "mock":      Provider("mock", "", None, "scripted", "scripted"),
}


def resolve(settings: Settings) -> Provider:
    """Fill in model ids the user did not pin, and fail loudly on a missing key."""
    provider = PROVIDERS.get(settings.provider)
    if provider is None:
        raise SystemExit(f"Unknown POCKET_PROVIDER={settings.provider!r}. "
                         f"Pick one of: {', '.join(PROVIDERS)}")
    settings.model = settings.model or provider.model
    settings.small_model = settings.small_model or provider.small_model
    if provider.kind != "mock" and not (settings.api_key or os.getenv(provider.key_env, "")):
        raise SystemExit(f"No API key. Set {provider.key_env} in your .env "
                         f"(or run with POCKET_PROVIDER=mock for the offline demo).")
    return provider


def get_client(settings: Settings):
    provider = resolve(settings)
    key = settings.api_key or os.getenv(provider.key_env, "")
    base_url = settings.base_url or provider.base_url
    if provider.kind == "mock":
        return ScriptedClient()
    if provider.kind == "anthropic":
        return AnthropicClient(api_key=key, base_url=base_url)
    return OpenAICompatClient(api_key=key, base_url=base_url)


def system_blocks(system: str | list[str]) -> list[dict]:
    """Anthropic reads the prompt as tools, then system, then messages, so ONE
    breakpoint at the end of the stable system block covers the tool schemas and
    the persona together — which is most of what a short turn sends.

    A caller that hands over a plain string gets the breakpoint at the end of it.
    That is honest but usually worthless: see `Session.build_system_parts` for
    why anything with a clock in it must come after the breakpoint, not before.
    """
    parts = [system] if isinstance(system, str) else [p for p in system if p]
    blocks = [{"type": "text", "text": part} for part in parts if part]
    if blocks:
        blocks[0]["cache_control"] = {"type": "ephemeral"}
    return blocks


class AnthropicClient:
    """The SDK plus the one thing the loop needs from it that it will not do on
    its own: mark the stable prefix cacheable. Usage comes back unchanged, so
    `cache_read_input_tokens` reaches the ledger."""

    def __init__(self, api_key: str, base_url: str | None = None,
                 max_retries: int = MAX_RETRIES, timeout: float = 120.0):
        import anthropic

        kwargs: dict = {"api_key": api_key, "max_retries": max_retries, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**kwargs)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, *, model, messages, max_tokens, system=None, tools=None):
        kwargs: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if system:
            kwargs["system"] = system_blocks(system)
        if tools:
            kwargs["tools"] = tools
        return self._client.messages.create(**kwargs)


# --------------------------------------------------------------------------
# wire format #2: OpenAI chat.completions <-> Anthropic Messages
# --------------------------------------------------------------------------
class OpenAICompatClient:
    """Speaks the Anthropic Messages shape the loop expects, backed by an
    OpenAI-style chat.completions endpoint. The translation is the whole class:
    system prompt becomes a message, content blocks become tool_calls, and
    tool_result blocks become 'tool' messages."""

    def __init__(self, api_key: str, base_url: str | None = None, timeout: float = 120.0):
        import openai

        self._client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout,
                                     max_retries=MAX_RETRIES)
        self.messages = SimpleNamespace(create=self._create)

    @staticmethod
    def _flatten(system) -> str:
        """OpenAI-shaped endpoints cache automatically and take one system
        message, so the split this repo makes for Anthropic just gets joined."""
        return system if isinstance(system, str) else "\n".join(p for p in system if p)

    def _to_openai(self, *, model, messages, max_tokens, system=None, tools=None) -> dict:
        out = [{"role": "system", "content": self._flatten(system)}] if system else []
        for message in messages:
            content = message["content"]
            if isinstance(content, str):
                out.append({"role": message["role"], "content": content})
            elif message["role"] == "assistant":
                text = "".join(b.text for b in content if getattr(b, "type", "") == "text")
                calls = [{"id": b.id, "type": "function",
                          "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                         for b in content if getattr(b, "type", "") == "tool_use"]
                entry: dict = {"role": "assistant", "content": text or None}
                if calls:
                    entry["tool_calls"] = calls
                out.append(entry)
            else:
                for block in content:      # tool_result blocks -> one 'tool' message each
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        out.append({"role": "tool", "tool_call_id": block["tool_use_id"],
                                    "content": block["content"]})
        kwargs: dict = {"model": model, "messages": out, "max_completion_tokens": max_tokens}
        if tools:
            kwargs["tools"] = [{"type": "function",
                                "function": {"name": t["name"], "description": t["description"],
                                             "parameters": t["input_schema"]}} for t in tools]
        return kwargs

    def _create(self, *, model, messages, max_tokens, system=None, tools=None):
        kwargs = self._to_openai(model=model, messages=messages, max_tokens=max_tokens,
                                 system=system, tools=tools)
        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            # older OpenAI-compatible endpoints only know `max_tokens`. Retry ONLY
            # when the error is about that parameter, so real failures stay visible.
            if "max_completion_tokens" not in str(exc).lower():
                raise
            kwargs["max_tokens"] = kwargs.pop("max_completion_tokens")
            response = self._client.chat.completions.create(**kwargs)
        choice = response.choices[0].message
        blocks = []
        if choice.content:
            blocks.append(SimpleNamespace(type="text", text=choice.content))
        for call in choice.tool_calls or []:
            blocks.append(SimpleNamespace(type="tool_use", id=call.id, name=call.function.name,
                                          input=json.loads(call.function.arguments or "{}")))
        usage = getattr(response, "usage", None)
        return SimpleNamespace(
            stop_reason="tool_use" if choice.tool_calls else "end_turn",
            usage=SimpleNamespace(input_tokens=getattr(usage, "prompt_tokens", 0),
                                  output_tokens=getattr(usage, "completion_tokens", 0)),
            content=blocks)


# --------------------------------------------------------------------------
# the offline stub
# --------------------------------------------------------------------------
def _block(**kwargs):
    return SimpleNamespace(**kwargs)


def _reply(blocks, tool_use: bool, tokens: int = 40):
    return SimpleNamespace(
        stop_reason="tool_use" if tool_use else "end_turn",
        usage=SimpleNamespace(input_tokens=tokens, output_tokens=tokens // 2),
        content=blocks)


class ScriptedClient:
    """A deterministic stand-in for a model, in ~80 lines of rules.

    It answers the four prompts the harness sends — retrieval gate, triage,
    consolidation summariser, and a loop turn — well enough that every seam
    (gate -> memory -> loop -> tool -> database -> trace) can be exercised
    offline and asserted on. It has no intelligence and makes no claim to.
    """

    def __init__(self):
        self.messages = SimpleNamespace(create=self._create)
        self.calls = 0

    def _create(self, *, model, messages, max_tokens, system=None, tools=None):
        self.calls += 1
        if not isinstance(system, (str, type(None))):
            system = "\n".join(p for p in system if p)
        last = messages[-1]["content"]
        text = last if isinstance(last, str) else ""
        if "retrieval gate" in text:
            return self._gate(text)
        if "triage gate" in text:
            return self._triage(text)
        if "long-term memory" in text and "Exchanges:" in text:
            return self._consolidate(text)
        if "warm and concise assistant" in text:
            return _reply([_block(type="text", text="Anytime. What else can I do?")], False)
        return self._turn(messages, tools)

    # -- one-shot JSON judges -------------------------------------------------
    @staticmethod
    def _user_line(prompt: str) -> str:
        return prompt.rsplit(":", 1)[-1].strip().lower()

    # filler words that carry no memory need, so "what is 2+2?" reduces to "2+2"
    _FILLER: ClassVar[set[str]] = {
        "what", "whats", "what's", "is", "the", "of", "calculate", "compute",
        "how", "much", "please", "tell", "me"}

    def _gate(self, prompt: str):
        message = self._user_line(prompt)
        rest = " ".join(w for w in re.split(r"[\s?]+", message) if w and w not in self._FILLER)
        pure_math = bool(re.search(r"\d", rest)) and bool(re.fullmatch(r"[\d\s+\-*/x×=.()]*", rest))
        retrieve = not (pure_math or message.strip(" !.?") in {"hi", "hello", "thanks", "thank you"})
        decision = {"retrieve": bool(retrieve), "query": message if retrieve else "",
                    "reason": "references user's life" if retrieve else "self-contained"}
        return _reply([_block(type="text", text=json.dumps(decision))], False)

    def _triage(self, prompt: str):
        message = self._user_line(prompt)
        quick = message.strip(" !.?") in {"hi", "hello", "hey", "thanks", "thank you", "ok"}
        return _reply([_block(type="text", text=json.dumps(
            {"route": "quick" if quick else "full",
             "reason": "small talk" if quick else "needs tools"}))], False)

    def _consolidate(self, prompt: str):
        log = prompt.split("Exchanges:", 1)[1]
        facts = [{"subject": "user", "content": line.split(":", 1)[1].strip()}
                 for line in log.splitlines()
                 if line.startswith("user:") and "remember" in line.lower()]
        return _reply([_block(type="text", text=json.dumps(
            {"facts": facts, "episode": "talked to the assistant"}))], False)

    # -- a loop turn ----------------------------------------------------------
    def _turn(self, messages, tools):
        names = {t["name"] for t in (tools or [])}
        # a tool already ran this turn -> observe the result and reply (iteration 2)
        for message in reversed(messages):
            content = message["content"]
            if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                outputs = " ".join(b["content"] for b in content
                                   if isinstance(b, dict) and b.get("type") == "tool_result")
                return _reply([_block(type="text", text=f"Done. {outputs}")], False)
        user = next((m["content"] for m in reversed(messages)
                     if m["role"] == "user" and isinstance(m["content"], str)), "")
        low = user.lower()
        if "assign_team" in names and re.search(
                r"\b(and then|after that|also)\b.*\b(and then|after that|also)\b|"
                r"\bthree things\b|\bplan a\b", low):
            # the stub cannot plan; it proves the wiring, and parse_plan judges it
            return self._call("assign_team", {
                "goal": user[:80],
                "plan": json.dumps([
                    {"key": "remember", "task": f"Remember: {user[:60]}", "tools": "save_note"},
                    {"key": "book", "task": user[:60], "tools": "create_event"},
                    {"key": "confirm", "task": "What is on my calendar tomorrow?",
                     "tools": "list_events", "needs": "book"}])})
        # "remember ..." wins over "...meeting..." — the explicit ask comes first
        if "save_note" in names and re.search(r"^remember|remember that", low):
            subject = re.search(r"remember(?: that)?\s+(\w+)", low)
            return self._call("save_note", {"subject": subject.group(1) if subject else "user",
                                            "content": user})
        if "create_event" in names and re.search(r"schedule|book|meeting|catch-?up", low):
            return self._call("create_event", self._event_args(user))
        if "list_events" in names and re.search(r"calendar|what'?s on|schedule\?", low):
            from datetime import date, timedelta

            day = (date.today() + timedelta(days=1)).isoformat() if "tomorrow" in low else "today"
            return self._call("list_events", {"day": day})
        if "search_web" in names and re.search(r"^(search|look up|google)\b", low):
            query = re.sub(r"^(search for|search|look up|google)\s+", "", low).strip()
            return self._call("search_web", {"query": query})
        return _reply([_block(type="text", text="(scripted model) no tool matched this message.")],
                      False)

    @staticmethod
    def _event_args(user: str) -> dict:
        hour = 9
        match = re.search(r"(\d{1,2})\s*(am|pm)", user.lower())
        if match:
            hour = int(match.group(1)) % 12 + (12 if match.group(2) == "pm" else 0)
        from datetime import date, timedelta

        day = date.today() + timedelta(days=1)
        who = re.search(r"with (\w+)", user, re.IGNORECASE)
        return {"title": user.strip()[:60],
                "start": f"{day.isoformat()}T{hour:02d}:00",
                "attendees": who.group(1) if who else ""}

    def _call(self, name: str, args: dict):
        return _reply([_block(type="tool_use", id=f"call_{self.calls}", name=name, input=args)],
                      True)
