"""Judged evals — the other kind of question, kept in another file on purpose.

`evals.py` asks *did `create_event` fire with the right arguments, and did the
row land?* That is a unit test: 0 or 1, no model involved. This file asks *was
that a good reply?* and *was that gate decision defensible?* — questions whose
honest answer is a score, produced by a model.

Three rules keep the two from contaminating each other:

  separate      they never share a file and never share a runner, because the
                moment a scored case can turn the deterministic suite red, the
                suite stops being a gate and becomes a mood
  different     deterministic is a GATE (100%, one failure blocks the release);
                judged is a THRESHOLD (a score, always reported, blocking only
                when it falls under the line)
  skipped       with no key to run them, judged evals are SKIPPED, never passed.
                A suite that could not run must never look like one that did

One inversion is worth stating out loud: everywhere else in this repo a broken
judge FAILS OPEN, because a degraded part should cost latency and not
capability. Here it fails CLOSED. A grader that cannot grade must not hand out
marks — in an eval, "I could not tell" is a failure, not a pass.

Scoring is DeepEval's `GEval`, driven by a `DeepEvalBaseLLM` pointed at whichever
provider this assistant is already configured with — the grader costs no second
account and no second key. Writing our own rubric runner would have been fewer
lines and worse: chain-of-thought criteria, per-metric thresholds and score
reasons are a solved problem, and the interesting part of an eval suite is which
cases you choose, not the arithmetic that scores them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from pocket.memory import should_retrieve

# The grader must not phone home from a release gate. Set before DeepEval is
# imported anywhere, which is why this sits at module scope and not in a function.
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("ERROR_REPORTING", "NO")

# How much worse a missed retrieval is than a needless one. A false negative
# answers confidently from nothing; a false positive costs one local FTS5 query.
FALSE_NEGATIVE_COST = 4.0

def build_judge(client, model: str):
    """DeepEval's grader, wired to the provider pocket is already using.

    `DeepEvalBaseLLM` is the seam DeepEval offers for exactly this, and it is
    the same seam waku-agent uses. Note the ordering in `__init__`: the base
    class calls `load_model()` from its own constructor, so the client has to be
    on the instance before `super().__init__` runs.
    """
    from deepeval.models.base_model import DeepEvalBaseLLM

    class PocketJudge(DeepEvalBaseLLM):
        def __init__(self, client, model: str):
            self._client, self._model = client, model
            super().__init__(model=model)

        def load_model(self):
            return self._client

        def get_model_name(self) -> str:
            return f"pocket:{self._model}"

        def generate(self, prompt: str, schema=None):
            response = self._client.messages.create(
                model=self._model, max_tokens=1024,
                messages=[{"role": "user", "content": prompt}])
            text = "".join(b.text for b in response.content if b.type == "text")
            if schema is None:
                return text
            # reasoning models put a thinking block before the JSON, so slice to
            # the outermost braces rather than trusting the whole reply to parse
            return schema.model_validate_json(text[text.index("{"): text.rindex("}") + 1])

        async def a_generate(self, prompt: str, schema=None):
            return self.generate(prompt, schema)

    return PocketJudge(client, model)


@dataclass
class Verdict:
    name: str
    score: float
    threshold: float
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.score >= self.threshold

    def line(self) -> str:
        mark = "PASS" if self.passed else "UNDER"
        return f"  {mark:<5} {self.name:<34} {self.score:.2f} (>= {self.threshold:.2f})  {self.reason}"


@dataclass
class JudgedRun:
    verdicts: list[Verdict] = field(default_factory=list)
    skipped: str = ""

    @property
    def passed(self) -> bool:
        return all(v.passed for v in self.verdicts)

    def summary(self) -> dict:
        if self.skipped:
            return {"status": "skipped", "reason": self.skipped, "cases": 0}
        return {"status": "pass" if self.passed else "fail",
                "cases": len(self.verdicts),
                "under_threshold": [v.name for v in self.verdicts if not v.passed],
                "scores": {v.name: round(v.score, 3) for v in self.verdicts}}


# --------------------------------------------------------- reply quality
# Each case is a short conversation and ONE criterion. The criterion is written
# so that a lazy reply scores badly: "did not lie" is not the bar anywhere here.
RESPONSE_CASES = [
    {"name": "honours_a_remembered_preference",
     "setup": ["Remember that Alex prefers morning meetings"],
     "message": "Book a catch-up with Alex tomorrow",
     "criteria": ("The reply must confirm exactly one event was created, name Alex, and give a "
                  "time before noon — memory says Alex prefers mornings, so asking the user "
                  "what time, or picking an afternoon slot, scores 0.")},
    {"name": "does_not_invent_a_calendar",
     "setup": [],
     "message": "What's on my calendar tomorrow?",
     "criteria": ("The calendar is empty. The reply must say so plainly. Naming any event, or "
                  "implying it could not check, scores 0.")},
    {"name": "keeps_small_talk_small",
     "setup": [],
     "message": "thanks!",
     "criteria": ("One short, warm sentence. Listing capabilities, narrating tool calls, or "
                  "asking a question the user did not invite scores 0.")},
]

RESPONSE_THRESHOLD = 0.6


def score_reply(judge, name: str, message: str, reply: str, criteria: str) -> tuple[float, str]:
    """One GEval measurement. `judge` is whatever `build_judge` returned, or any
    stand-in with the same shape — the eval suite passes a broken one to prove
    this is the one place in the repo that fails CLOSED."""
    try:
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCase, LLMTestCaseParams

        metric = GEval(name=name, criteria=criteria, model=judge,
                       threshold=RESPONSE_THRESHOLD,
                       evaluation_params=[LLMTestCaseParams.INPUT,
                                          LLMTestCaseParams.ACTUAL_OUTPUT])
        metric.measure(LLMTestCase(input=message, actual_output=reply))
        return float(metric.score), str(metric.reason or "")[:80]
    except Exception as exc:
        return 0.0, f"judge failed closed ({type(exc).__name__}: {exc})"[:80]


# ----------------------------------------------------- retrieval gate accuracy
# Twelve cases, four shapes, three each. The interesting ones are the middle two:
# a follow-up is answerable from the window that is already in the prompt, and a
# question about something never stored still has to reach memory to find that out.
GATE_CASES = [
    ("hi", False, "chitchat"),
    ("thanks!", False, "chitchat"),
    ("ok sounds good", False, "chitchat"),
    ("what time does Alex like to meet?", True, "direct question"),
    ("what's my sister's name?", True, "direct question"),
    ("when is the Q4 offsite?", True, "direct question"),
    ("make it 30 minutes instead", False, "follow-up in history"),
    ("actually, move it to Thursday", False, "follow-up in history"),
    ("yes, do that", False, "follow-up in history"),
    ("what did I say about the Berlin trip?", True, "missing memory"),
    ("do I have a dentist appointment saved?", True, "missing memory"),
    ("what's my usual coffee order?", True, "missing memory"),
]

GATE_THRESHOLD = 0.5


def cost_weighted_accuracy(outcomes: list[tuple[bool, bool]]) -> float:
    """`outcomes` is (should_have_retrieved, did_retrieve) per case.

    A plain accuracy treats both mistakes as equal, and they are not: a missed
    retrieval answers confidently from nothing, while a needless one costs one
    local query. So a case whose truth is "retrieve" is worth
    FALSE_NEGATIVE_COST times as much as one whose truth is "skip", and the
    metric is the share of that weight the gate actually earned.
    """
    if not outcomes:
        return 0.0
    weight = [FALSE_NEGATIVE_COST if truth else 1.0 for truth, _ in outcomes]
    earned = sum(w for w, (truth, got) in zip(weight, outcomes, strict=True) if truth == got)
    return earned / sum(weight)


def judge_the_gate(client, small_model: str) -> Verdict:
    outcomes, misses = [], []
    for message, truth, shape in GATE_CASES:
        got, _query, _reason = should_retrieve(client, small_model, message)
        outcomes.append((truth, got))
        if truth != got:
            misses.append(f"{shape}: '{message[:28]}'")
    score = cost_weighted_accuracy(outcomes)
    reason = f"{len(GATE_CASES) - len(misses)}/{len(GATE_CASES)} correct"
    if misses:
        reason += " · missed " + "; ".join(misses[:2])
    return Verdict("retrieval_gate_cost_weighted", score, GATE_THRESHOLD, reason)


# ------------------------------------------------------------------ the runner
def run_judged(build_agent, client=None, small_model: str = "") -> JudgedRun:
    """`build_agent` returns a fresh assistant per case, so one case cannot leave
    memory or a calendar row behind for the next. Replies come from whichever
    model that assistant is configured with; grading is always the small one."""
    probe = build_agent()
    try:
        if probe.settings.provider == "mock":
            return JudgedRun(skipped="no provider key — the scripted stub cannot be graded")
        client = client or probe.client
        small_model = small_model or probe.settings.small_model
    finally:
        probe.close()

    # the gate cases need no grader at all: ground truth is written down, and the
    # only judgement in them is how much each kind of mistake is worth
    verdicts = [judge_the_gate(client, small_model)]
    judge = build_judge(client, small_model)      # the cheap model grades
    for case in RESPONSE_CASES:
        agent = build_agent()
        try:
            for line in case["setup"]:
                agent.respond(line)
            reply = agent.respond(case["message"]).reply
        finally:
            agent.close()
        score, reason = score_reply(judge, case["name"], case["message"], reply,
                                    case["criteria"])
        verdicts.append(Verdict(case["name"], score, RESPONSE_THRESHOLD, reason))
    return JudgedRun(verdicts=verdicts)


def scratch_agent():
    """Your provider and your key, but never your memory. The cases say
    "remember that Alex prefers mornings", and grading a suite must not leave
    that behind in the assistant you actually use."""
    import tempfile
    from pathlib import Path

    from pocket.agent import Pocket
    from pocket.config import Settings, load_settings

    settings = Settings(**{**vars(load_settings()),
                           "home": Path(tempfile.mkdtemp(prefix="pocket-judged-"))})
    return Pocket(settings=settings)


def main(build_agent=None) -> int:
    """`python -m pocket judge` — the scored suite on its own. It reports and
    returns non-zero when something is under threshold; the release gate in
    `evals.py` is what decides whether that blocks a release."""
    run = run_judged(build_agent or scratch_agent)
    if run.skipped:
        print(f"judged: skipped — {run.skipped}")
        return 0
    for verdict in run.verdicts:
        print(verdict.line())
    under = [v for v in run.verdicts if not v.passed]
    average = sum(v.score for v in run.verdicts) / len(run.verdicts)
    print(f"\njudged: {len(run.verdicts) - len(under)}/{len(run.verdicts)} at or above "
          f"threshold · mean {average:.2f}")
    return 1 if under else 0
