"""
WHAT:  The improvers: two proposers -- INV (how the role works) and DOM
       (what a practitioner knows) -- each rewriting the role's instructions
       from the cases, and a composer that keeps the edits the replays
       support.
WHY:   harness2's split: procedure and domain knowledge are different
       levers, and one proposer tends to do only one of them.
CONCEPT: Each is one model call on the `refiner` role, its instructions in
       prompts/refine/*.md. What it sees is clipped: a case's brief can be
       90k characters, and four of them would not fit.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from research_agent import prompt_files
from research_agent.refine.cases import Case
from research_agent.refine.replay import Output

#: Characters of each case a proposer sees.
BRIEF_CHARS, ANSWER_CHARS = 6000, 3000
#: The house limit for a role's instructions, and the hard one (Codex has
#: been measured to hang past about 6 KB of them).
LIMIT, HARD_LIMIT = 1400, 6000


class Note(BaseModel):
    change: str
    motivated_by: str
    """The case and the moment in it."""


class Proposal(BaseModel):
    keep_base: bool = False
    instructions: str = ""
    """The role's whole new instructions ("" with keep_base)."""
    notes: list[Note] = Field(default_factory=list, max_length=12)


def check(out: Proposal) -> list[str]:
    if out.keep_base:
        return []
    if not out.instructions.strip():
        return ["`instructions` is empty: write the whole new instructions, or keep_base"]
    if len(out.instructions) > HARD_LIMIT:
        return [f"`instructions` has {len(out.instructions)} characters; at most {HARD_LIMIT} "
                f"(they go with every message)"]
    return []


def clip(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    half = n // 2
    return text[:half] + f"\n[... {len(text) - n} characters left out ...]\n" + text[-half:]


def case_text(case: Case, outputs: list[Output] = ()) -> str:
    parts = [f"## Case {case.id}",
             f"### What it was sent (clipped)\n{clip(case.brief, BRIEF_CHARS)}",
             f"### What it answered then (clipped)\n{clip(case.reply, ANSWER_CHARS)}"]
    if case.problems:
        parts.append("### What code sent back, attempt by attempt\n"
                     + "\n".join(f"- {p}" for p in case.problems))
    parts.append("### What happened next\n" + (case.aftermath or "(nothing recorded)"))
    for o in outputs:
        state = ("passed the checks" if o.passed and not o.first_problems else
                 f"first answer failed the checks: {'; '.join(o.first_problems)}" if o.passed
                 else f"no usable answer: {o.error[:300]}")
        parts.append(f"### Replayed with the {o.label} instructions -- {state}\n"
                     f"{clip(o.answer, ANSWER_CHARS)}")
    return "\n\n".join(parts)


def _brief(base: str, cases: list[Case], outputs: dict, lessons: str, extra: str = "") -> str:
    parts = [f"=== THE CURRENT INSTRUCTIONS ===\n{base}"]
    if lessons.strip():
        parts.append(f"=== LESSONS FROM EARLIER REFINEMENTS OF THIS ROLE ===\n{lessons}")
    if extra:
        parts.append(extra)
    parts.append("=== THE CASES ===\n\n" + "\n\n".join(
        case_text(c, outputs.get(c.id, [])) for c in cases))
    return "\n\n".join(parts)


def propose(run, dimension: str, *, role: str, base: str, cases: list[Case],
            baseline: dict, lessons: str = "") -> Proposal:
    """One proposer's rewrite: dimension "inv" or "dom"."""
    instructions = prompt_files.text(f"refine/propose_{dimension}", run.folder, role=role,
                                     limit=LIMIT)
    out, _ = run.call("refiner", brief=_brief(base, cases, baseline, lessons),
                      schema=Proposal, skills=None, instructions=instructions, check=check,
                      label=f"propose {dimension}")
    return out


def compose(run, *, role: str, base: str, proposals: dict, cases: list[Case],
            outputs: dict) -> Proposal:
    """The composer: `proposals` label -> Proposal; `outputs` case id ->
    every replay (base and the proposals')."""
    shown = "\n\n".join(f"=== PROPOSAL {label.upper()} ===\n"
                        + ("(keep the base)" if p.keep_base else p.instructions)
                        + "".join(f"\n- {n.change} (because {n.motivated_by})" for n in p.notes)
                        for label, p in proposals.items())
    instructions = prompt_files.text("refine/compose", run.folder, role=role, limit=LIMIT)
    out, _ = run.call("refiner", brief=_brief(base, cases, outputs, "", shown), schema=Proposal,
                      skills=None, instructions=instructions, check=check, label="compose")
    return out
