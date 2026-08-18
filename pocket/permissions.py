"""Permissions — the last thing between a model's intent and your machine.

Three rules, in this order, and they are all readable:

  1. a DENY pattern matches            -> refuse, always, no prompt
  2. the tool is marked `risk="ask"`   -> ask the human (CLI prompt, or a
                                          callback a gateway supplies)
  3. otherwise                          -> allow

Everything an MCP server or a sub-agent brings in is `ask` by default: it is
third-party code reached through a tool call, and the honest default for third
-party code is "a human sees it first". A refusal is returned to the model as
text, exactly like a tool error, so the turn continues and the model can pick a
different route instead of the process dying.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field

# Patterns that are never allowed, whatever the mode. Deliberately short: a long
# blocklist reads as security and behaves as theatre. These are the shapes that
# have no legitimate use inside an assistant's tool call.
DENY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"rm\s+-rf?\s+[~/]", "recursive delete of a home or root path"),
    (r":\(\)\s*\{.*\}\s*;\s*:", "fork bomb"),
    (r"\b(curl|wget)\b[^|]*\|\s*(ba)?sh", "pipe-to-shell of remote code"),
    (r"(id_rsa|id_ed25519|\.aws/credentials|\.ssh/|\.env\b)", "reads a credential file"),
    (r"\bDROP\s+TABLE\b", "destructive SQL"),
)


@dataclass
class Decision:
    verdict: str          # allow | deny | asked
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict in ("allow", "asked")


@dataclass
class Policy:
    """`confirm` is injected: the CLI passes a prompt, a bot passes a button, the
    eval suite passes a lambda. The policy itself never touches stdin."""

    confirm: Callable[[str, dict, str], bool] | None = None
    # tool names the user has pre-trusted for this session (POCKET_TRUST=a,b)
    trusted: set[str] = field(default_factory=lambda: {
        name.strip() for name in os.getenv("POCKET_TRUST", "").split(",") if name.strip()})
    # remembered "yes" answers, so one confirmation covers a whole turn's retries
    granted: set[str] = field(default_factory=set)

    def check(self, name: str, args: dict, risk: str = "safe") -> Decision:
        blob = f"{name} {args}"
        for pattern, why in DENY_PATTERNS:
            if re.search(pattern, blob, re.IGNORECASE):
                return Decision("deny", why)
        if risk != "ask" or name in self.trusted or name in self.granted:
            return Decision("allow")
        if self.confirm is None:
            return Decision("deny", "needs confirmation but nothing can ask the human")
        if self.confirm(name, args, risk):
            self.granted.add(name)
            return Decision("asked", "human approved")
        return Decision("deny", "human declined")


def cli_confirm(name: str, args: dict, risk: str) -> bool:
    """The terminal's answer to `Policy.confirm`. Anything but 'y' is a no."""
    print(f"\n  permission · {name}({args}) wants to run [{risk}]")
    try:
        return input("  allow it? [y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False
