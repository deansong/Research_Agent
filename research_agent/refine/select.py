"""
WHAT:  Blind selection: for each case, which candidate instructions made the
       best answer -- judged on the answers alone, shuffled and unlabelled --
       and the tally that decides which instructions win.
WHY:   There is no ground truth for a design or a decision. harness2's
       answer: compare the outputs against the request and its material,
       never knowing which instructions made which.
CONCEPT: A candidate whose replay gave no usable answer is not shown: code
       already judged it. The base wins ties: new instructions are adopted
       only with more case wins than the base and more than any other.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from pydantic import BaseModel

from research_agent import prompt_files
from research_agent.refine.cases import Case
from research_agent.refine.propose import ANSWER_CHARS, BRIEF_CHARS, clip
from research_agent.refine.replay import Output

#: How much of the request the judge sees: more than a proposer, one case
#: at a time.
REQUEST_CHARS = 4 * BRIEF_CHARS


class Choice(BaseModel):
    why: str
    choice: str
    """The id of the single best candidate (A, B, ...)."""


@dataclass
class Pick:
    case: str
    winner: str
    """The label of the instructions that won, "" if none could be judged."""
    why: str = ""
    shown: dict = field(default_factory=dict)
    """Anonymous id -> label, for the report."""


def judge(run, case: Case, outputs: list[Output], *, seed: int = 0) -> Pick:
    """Pick the best answer to `case` among the replays that answered."""
    usable = [o for o in outputs if o.passed]
    if not usable:
        return Pick(case=case.id, winner="", why="no candidate gave a usable answer")
    if len(usable) == 1:
        return Pick(case=case.id, winner=usable[0].label,
                    why="the only candidate with a usable answer", shown={"A": usable[0].label})
    random.Random(f"{seed}:{case.id}").shuffle(usable)
    ids = {chr(ord("A") + i): o for i, o in enumerate(usable)}
    candidates = "\n\n".join(
        f"=== CANDIDATE {i} -- "
        + ("passed the checks" if not o.first_problems else
           f"passed the checks after a retry (first: {'; '.join(o.first_problems)[:300]})")
        + f" ===\n{clip(o.answer, 2 * ANSWER_CHARS)}" for i, o in ids.items())
    brief = (f"=== THE REQUEST (what the role was sent; clipped) ===\n"
             f"{clip(case.brief, REQUEST_CHARS)}\n\n{candidates}")

    def check(out: Choice) -> list[str]:
        return [] if out.choice.strip() in ids else [
            f"`choice` must be one of {', '.join(ids)}, not {out.choice!r}"]

    out, _ = run.call("refiner", brief=brief, schema=Choice, skills=None,
                      instructions=prompt_files.text("refine/select", run.folder, role=case.role),
                      check=check, label=f"judge {case.id}")
    return Pick(case=case.id, winner=ids[out.choice.strip()].label, why=out.why,
                shown={i: o.label for i, o in ids.items()})


def tally(picks: list[Pick], base: str = "base") -> tuple[str, dict]:
    """(the winning label, wins per label). The base keeps its place unless
    another label has more wins than it and than every other."""
    wins: dict[str, int] = {}
    for p in picks:
        if p.winner:
            wins[p.winner] = wins.get(p.winner, 0) + 1
    best = max(wins.values(), default=0)
    leaders = [label for label, n in wins.items() if n == best]
    if len(leaders) != 1 or leaders[0] == base or best <= wins.get(base, 0):
        return base, wins
    return leaders[0], wins
