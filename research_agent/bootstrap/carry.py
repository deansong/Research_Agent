"""
WHAT:  Starting a run from an earlier one: its research state -- with the
       reviewer's review -- carried into a new run folder, for the bootstrap's
       planner and designer to revise.
WHY:   A review asks for more: more seeds, a missing baseline, an ablation.
       That changes the plan and the design, which the user approves -- so the
       next run goes back through the bootstrap, starting from everything the
       last one knew instead of from nothing.
CONCEPT: What carries over, and what is approved again:

    carried       goal, constraints, assumptions, evidence, hypotheses (with
                  their results), rubric, protocol, plan, design, decisions,
                  open questions, risks, reviews
    left behind   the step table and the node calls (the review keeps what
                  was run; the old folder keeps the files)
    approvals     hypotheses, rubric and protocol stay approved (a revision
                  of them lapses it, as always); the plan and the design must
                  be approved again
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from research_agent import state as S

CARRIED = ("goal", "constraints", "assumptions", "evidence", "hypotheses", "rubric", "protocol",
           "plan", "design", "decisions", "open_questions", "risks", "reviews")
STILL_APPROVED = ("hypotheses", "rubric", "protocol")


def next_folder(old: Path) -> Path:
    """<old>-next, or -next2, -next3 ... beside it."""
    for n in range(1, 100):
        folder = old.parent / f"{old.name}-next{n if n > 1 else ''}"
        if not folder.exists():
            return folder
    raise ValueError(f"Too many runs already follow {old}.")


def carried_state(old: Path) -> dict:
    """The new run's research state, from the old run's."""
    st = S.Store(old).read()
    if not (st.get("goal") or {}).get("question"):
        raise ValueError(f"{old} has no research state to start from.")
    new = S.new()
    for section in CARRIED:
        new[section] = json.loads(json.dumps(st.get(section, new[section])))
    new["versions"] = {s: v for s, v in (st.get("versions") or {}).items() if s in CARRIED}
    new["approvals"] = {s: v for s, v in (st.get("approvals") or {}).items()
                        if s in STILL_APPROVED and S.approved(st, s)}
    reviews = [r for r in new.get("reviews") or []
               if r.get("run") and Path(r["run"]).expanduser().resolve() == old.resolve()]
    asked = "; ".join(f"[{q['priority']}] {q['what']}" for r in reviews
                      for q in r.get("requests") or [])
    summary = (f"This run continues {old}. " + (st.get("status") or {}).get("summary", "")
               + (f" Its review ({', '.join(r['id'] for r in reviews)}) asks for: {asked}"
                  if reviews else " It was not reviewed."))
    new["status"] = {"phase": "discussing", "summary": summary.strip(),
                     "next": "revise the plan and the design for the review"}
    new, _ = S.apply(new, [{"op": "add", "section": "decisions", "value": {
        "what": f"Start from run {old.name}" + (f" and its review {reviews[-1]['id']}"
                                                if reviews else ""),
        "why": "the next run addresses what the last one left open", "by": "user"}}],
        by="bootstrap")
    return new


def start_from(old: Path, folder: Path) -> dict:
    """Make the new run folder: the carried state, and the old run's model
    choices (config.json, execute.json) unless the folder has its own."""
    old = Path(old).expanduser().resolve()
    if not (old / "state" / "research.json").exists():
        raise ValueError(f"{old} is not a run folder (no state/research.json).")
    st = carried_state(old)
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "execute.json"):
        if (old / name).exists() and not (folder / name).exists():
            shutil.copy(old / name, folder / name)
    S.Store(folder).write(st, by="bootstrap", ops=[{"op": "carry", "from": str(old)}],
                          changed=list(CARRIED))
    return st


def first_message(old: Path) -> str:
    return (f"Continue the study from the previous run ({Path(old).expanduser()}). Its results "
            f"and the reviewer's review are in the research state. Revise the plan -- and the "
            f"hypotheses, rubric or protocol only where the review needs it -- and the design, "
            f"to answer the review.")
