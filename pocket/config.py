"""Configuration — one dataclass, every knob an env var. No settings framework.

Reading this file tells you everything pocket can be configured to do.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """A .env loader in eight lines, so the core needs zero dependencies.
    Real env vars win: `POCKET_PROVIDER=openai python -m pocket` overrides .env."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _default_provider() -> str:
    """Pick a provider that can actually run right now.

    With no key anywhere we fall back to the scripted offline model, so
    `python -m pocket demo` and `eval` work on a fresh clone with no network.
    That model is a stub, not intelligence — see models.ScriptedClient.
    """
    chosen = os.getenv("POCKET_PROVIDER", "").strip()
    if chosen:
        return chosen
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY"):
        if os.getenv(env):
            return {"ANTHROPIC_API_KEY": "anthropic", "OPENAI_API_KEY": "openai",
                    "DEEPSEEK_API_KEY": "deepseek", "MOONSHOT_API_KEY": "kimi"}[env]
    return "mock"


@dataclass
class Settings:
    # --- brain
    provider: str = field(default_factory=_default_provider)
    api_key: str = field(default_factory=lambda: os.getenv("POCKET_API_KEY", ""))
    base_url: str | None = field(default_factory=lambda: os.getenv("POCKET_BASE_URL") or None)
    model: str = field(default_factory=lambda: os.getenv("POCKET_MODEL", ""))
    # the cheap model that runs the retrieval gate, triage and consolidation
    small_model: str = field(default_factory=lambda: os.getenv("POCKET_SMALL_MODEL", ""))

    # --- where state lives. One directory you can open, delete, or back up.
    home: Path = field(default_factory=lambda: Path(os.getenv("POCKET_HOME", ".pocket")))

    # --- loop guardrails
    max_iterations: int = field(default_factory=lambda: int(os.getenv("POCKET_MAX_ITERATIONS", "8")))
    max_tokens: int = field(default_factory=lambda: int(os.getenv("POCKET_MAX_TOKENS", "4096")))
    # working memory is a sliding window: only the last N turns enter the prompt
    history_turns: int = field(default_factory=lambda: int(os.getenv("POCKET_HISTORY_TURNS", "8")))

    # --- memory
    consolidate_every: int = field(default_factory=lambda: int(os.getenv("POCKET_CONSOLIDATE_EVERY", "3")))
    retrieval_top_k: int = field(default_factory=lambda: int(os.getenv("POCKET_RETRIEVAL_TOP_K", "4")))

    # --- context engineering
    # a tool result longer than this is written to .pocket/artifacts/ and replaced
    # by a preview plus a pointer (the model reads the rest on demand)
    tool_result_limit: int = field(
        default_factory=lambda: int(os.getenv("POCKET_TOOL_RESULT_LIMIT", "2000")))
    # when the conversation in the prompt exceeds this, the oldest turns are
    # folded into one summarised turn
    context_budget_chars: int = field(
        default_factory=lambda: int(os.getenv("POCKET_CONTEXT_BUDGET", "12000")))
    # register the `delegate` tool (a sub-agent with a scoped tool set)
    subagents: bool = field(
        default_factory=lambda: os.getenv("POCKET_SUBAGENTS", "1") in ("1", "true", "yes"))
    # register the `assign_team` tool (several workers over one shared board).
    # Off by default: every registered tool ships in every prompt, and a team is
    # the heaviest thing this assistant can start.
    team: bool = field(
        default_factory=lambda: os.getenv("POCKET_TEAM", "") in ("1", "true", "yes"))

    # --- graph front door (off = plain loop; on = triage graph, fails open)
    graph_workflows: bool = field(
        default_factory=lambda: os.getenv("POCKET_GRAPH_WORKFLOWS", "") in ("1", "true", "yes"))

    def ensure_home(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "traces").mkdir(exist_ok=True)
        return self.home


def load_settings() -> Settings:
    load_dotenv()
    return Settings()
