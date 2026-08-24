"""Measure what is actually measurable offline. No estimates dressed as data."""
import pathlib
import tempfile

from pocket.agent import Pocket
from pocket.config import Settings
from pocket.context import PREVIEW_CHARS, compact_history, offload_if_large
from pocket.models import ScriptedClient

CHARS_PER_TOKEN = 4  # the usual English approximation; stated, not hidden


def agent(**kw):
    return Pocket(settings=Settings(provider="mock",
                                    home=pathlib.Path(tempfile.mkdtemp(prefix="measure-")), **kw),
                  client=ScriptedClient(), confirm=lambda *a: True)


print("=" * 72)
print("A. SKILL DISCLOSURE  (level 1 catalog vs level 2 bodies)")
print("=" * 72)
p = agent()
loader = p.memory.skills
catalog = loader.catalog()
bodies = sum(len(s.body) for s in loader.skills)
per_line = [len(s.catalog_line()) for s in loader.skills]
print(f"skills shipped:            {len(loader.skills)}")
print(f"catalog total:             {len(catalog):>6} chars  (~{len(catalog)//CHARS_PER_TOKEN} tok)")
print(f"catalog per skill:         {sum(per_line)/len(per_line):>6.0f} chars/skill")
print(f"bodies per skill:          {bodies/len(loader.skills):>6.0f} chars/skill")
print(f"body:catalog ratio:        {bodies/sum(per_line):>6.1f}x")

system_now = p.session.build_system("what is 2+2?")
inlined = system_now + "\n\n".join(f"### {s.name}\n{s.body}" for s in loader.skills)
print(f"\nsystem prompt now:         {len(system_now):>6} chars "
      f"(~{len(system_now)//CHARS_PER_TOKEN} tok)")
print(f"if bodies were inlined:    {len(inlined):>6} chars "
      f"(~{len(inlined)//CHARS_PER_TOKEN} tok)")
print(f"saved with 1 skill:        {100 * (1 - len(system_now)/len(inlined)):>6.1f}%")
for n in (10, 25, 50):
    resident = len(system_now) + (n - 1) * (sum(per_line) / len(per_line))
    all_in = len(system_now) + (n - 1) * (bodies / len(loader.skills)) + \
        (n - 1) * (sum(per_line) / len(per_line))
    print(f"  extrapolated to {n:>2} skills: {resident/CHARS_PER_TOKEN/1000:>5.1f}k tok resident "
          f"vs {all_in/CHARS_PER_TOKEN/1000:>5.1f}k inlined "
          f"({100 * (1 - resident/all_in):>4.1f}% saved)")
p.close()

print()
print("=" * 72)
print("B. CONTEXT GOVERNANCE")
print("=" * 72)
p = agent(tool_result_limit=2000)
big = "x" * 40_000
shown = offload_if_large("firehose", big, p.settings.home, p.settings.tool_result_limit)
print(f"offloading a 40KB tool result:")
print(f"  raw:                     {len(big):>6} chars (~{len(big)//CHARS_PER_TOKEN} tok)")
print(f"  what the prompt sees:    {len(shown):>6} chars (~{len(shown)//CHARS_PER_TOKEN} tok)")
print(f"  reduction:               {100 * (1 - len(shown)/len(big)):>6.1f}%   "
      f"(preview {PREVIEW_CHARS} chars + a pointer)")
p.close()

TURN = 900  # chars of one realistic exchange (user + assistant + tool summary)
for turns in (10, 20, 40):
    p = agent()
    p.session.history = []
    for i in range(turns):
        p.session.history.append({"role": "user", "content": f"turn {i}: " + "u" * (TURN // 3)})
        p.session.history.append({"role": "assistant", "content": f"reply {i}: " + "a" * (TURN // 2)})
    raw = p.session.messages_for("and now?")
    raw_chars = sum(len(str(m["content"])) for m in raw)
    windowed = raw_chars
    compacted, folded = compact_history(raw, p.settings.context_budget_chars, p.summarise)
    comp_chars = sum(len(str(m["content"])) for m in compacted)
    unbounded = sum(TURN for _ in range(turns)) + 900
    print(f"\n{turns:>2}-turn session:")
    print(f"  everything, no window: {unbounded:>6} chars (~{unbounded//CHARS_PER_TOKEN} tok)")
    print(f"  after the window:      {windowed:>6} chars (last {p.settings.history_turns} turns)")
    print(f"  after compaction:      {comp_chars:>6} chars, {folded} messages folded")
    print(f"  total reduction:       {100 * (1 - comp_chars/unbounded):>6.1f}%")
    p.close()

print()
print("=" * 72)
print("C. THE GATE")
print("=" * 72)
import pocket.evals as ev
import pocket.judge as jd
print(f"deterministic cases:       {len(ev.CASES)}")
print(f"judged verdicts per run:   {1 + len(jd.RESPONSE_CASES)} "
      f"({len(jd.GATE_CASES)} gate cases -> 1 cost-weighted verdict, "
      f"{len(jd.RESPONSE_CASES)} reply-quality verdicts)")
print(f"gate threshold:            deterministic 100%, judged >= "
      f"{jd.RESPONSE_THRESHOLD} / {jd.GATE_THRESHOLD}")
print(f"false-negative price:      {jd.FALSE_NEGATIVE_COST}x a false positive")
