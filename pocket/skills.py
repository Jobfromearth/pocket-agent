"""Skills — procedural memory, disclosed in two levels instead of one.

A skill is a `SKILL.md`: frontmatter with `name` and `description`, then a body
of instructions. The whole mechanism exists to answer one question — *how does
capability grow without the default prompt growing with it?*

    level 1   every skill's NAME and DESCRIPTION are scanned once at startup and
              rendered as a catalog that always ships in the system prompt. It
              costs a line per skill, and it is what makes the second level
              possible: the model cannot ask for a body it does not know exists
    level 2   a BODY enters the prompt only when it is wanted, and it enters as
              its OWN message rather than being concatenated into the system
              prompt. That isolation is the point: a skill body is instructions
              for one job, it should be attributable in the trace, and it should
              be droppable by compaction without taking the system prompt with it

Two things can pull a body in, and they are not rivals:

    the matcher   keyword overlap with the catalog line. Cheap, transparent —
                  you can compute the score in your head, which is why you can
                  debug why a skill did or did not fire — and it costs no extra
                  round trip when it is right
    `read_skill`  the model asks by name. This is the path that matters when the
                  matcher is wrong, and the only path that works for a language
                  the matcher cannot tokenise

That second path is why the matcher is allowed to be dumb. It used to be the
only path, and a matcher that cannot tokenise a language silently removed every
skill from every turn in it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pocket.tools import Tool

MAX_AUTO_SKILLS = 2
# Scripts without spaces between words: one CJK character is a token, and so is
# every adjacent pair, which is enough to match a catalog line without a
# segmenter. The same reasoning as `fts_query` in memory.py, for the same reason.
_UNSEGMENTED = re.compile("[぀-ヿ㐀-䶿一-鿿]")
_WORDS = re.compile(r"[^\W_]{2,}")


def tokens(text: str) -> set[str]:
    """Words for a spaced language, characters and bigrams for one without."""
    found: set[str] = set()
    for word in _WORDS.findall(text.lower()):
        if _UNSEGMENTED.search(word):
            found.update(word)
            found.update(word[i:i + 2] for i in range(len(word) - 1))
        else:
            found.add(word)
    return found


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: str = ""

    def catalog_line(self) -> str:
        return f"- {self.name}: {self.description}"


class SkillLoader:
    def __init__(self, dirs: list[Path]):
        self.dirs = dirs
        self.skills: list[Skill] = []
        self.refresh()

    def refresh(self) -> None:
        self.skills = []
        for directory in self.dirs:
            for path in sorted(directory.rglob("SKILL.md")) if directory.is_dir() else []:
                skill = self._parse(path.read_text(encoding="utf-8"))
                if skill:
                    skill.path = str(path)
                    self.skills.append(skill)

    @staticmethod
    def _parse(text: str) -> Skill | None:
        match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
        if not match:
            return None
        front, body = match.groups()
        fields = {k.strip(): v.strip().strip("'\"")
                  for k, _, v in (line.partition(":") for line in front.splitlines() if ":" in line)}
        if "name" not in fields or "description" not in fields:
            return None
        return Skill(fields["name"], fields["description"], body.strip())

    # ---- level 1
    def catalog(self) -> str:
        """What always ships. One line each, and an instruction for the case the
        matcher gets wrong — without this the second level has no door."""
        if not self.skills:
            return ""
        return ("\nSkills you can follow. Their instructions are NOT loaded yet: call "
                "read_skill(name) to load one before doing a job it covers.\n"
                + "\n".join(s.catalog_line() for s in self.skills))

    def get(self, name: str) -> Skill | None:
        wanted = name.strip().lower()
        return next((s for s in self.skills if s.name.lower() == wanted), None)

    # ---- level 2
    def match(self, message: str, max_skills: int = MAX_AUTO_SKILLS) -> list[Skill]:
        """Keyword overlap with name + description. No embeddings — the score is
        checkable by eye, which is the whole reason to prefer it here."""
        words = tokens(message)
        scored = []
        for skill in self.skills:
            overlap = len(words & tokens(f"{skill.name} {skill.description}"))
            if overlap >= 2:
                scored.append((overlap, skill))
        scored.sort(key=lambda pair: -pair[0])
        return [skill for _, skill in scored[:max_skills]]


def as_message(skill: Skill) -> dict:
    """A body enters as its own message, attributed and droppable, never folded
    into the system prompt."""
    return {"role": "user",
            "content": f"[skill: {skill.name}]\n{skill.body}"}


def make_read_skill_tool(loader: SkillLoader) -> Tool:
    def read_skill(name: str) -> str:
        skill = loader.get(name)
        if skill is None:
            available = ", ".join(s.name for s in loader.skills) or "none"
            return f"Error: no skill named '{name}'. Available: {available}"
        return f"[skill: {skill.name}]\n{skill.body}"

    return Tool(
        name="read_skill",
        description=("Load the full instructions for one skill from the catalog in your system "
                     "prompt. Call it before doing a job a skill covers, unless its body is "
                     "already in this conversation."),
        input_schema={"type": "object", "properties": {
            "name": {"type": "string", "description": "the skill's name, exactly as catalogued"}},
            "required": ["name"]},
        fn=read_skill)
