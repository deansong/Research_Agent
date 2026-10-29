"""
WHAT:  Starting a run from an earlier one: its research state -- with the
       reviewer's review -- carried into a new run folder, for the bootstrap's
       designer to revise.
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
    approvals     none: the revised design is approved again, as one
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from research_agent import state as S

CARRIED = ("goal", "constraints", "assumptions", "evidence", "brief", "hypotheses", "rubric",
           "protocol",
           "plan", "design", "decisions", "open_questions", "risks", "reviews")
STILL_APPROVED: tuple[str, ...] = ()


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
                     "next": "go through the paper and the review with the user, then revise "
                             "the brief and the design", "previous": str(old)}
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
    return (f"Continue the study from the previous run ({Path(old).expanduser()}). Go through "
            f"its paper and the review with me: what the review asks for, what you would "
            f"change and what you would keep. Then we revise the brief, and the design from it.")


#: Characters of the last run's paper shown to the discussor.
PAPER_CHARS = 12000


def last_run(st: dict, budget: int | None = None) -> str:
    """The run this one continues, for the discussor: its paper (the text)
    and its review in full. "" when it continues none."""
    old = (st.get("status") or {}).get("previous")
    if not old:
        return ""
    old = Path(old)
    paper = ""
    for name in ("paper.md", "main.tex"):
        if (old / "paper" / name).is_file():
            paper = (old / "paper" / name).read_text(errors="replace")
            break
    limit = PAPER_CHARS if budget is None else max(min(PAPER_CHARS, budget // 2), 2000)
    if len(paper) > limit:
        paper = paper[:limit] + f"\n[... the rest is in {old / 'paper'}]"
    reviews = [r for r in st.get("reviews") or []
               if r.get("run") and Path(r["run"]).resolve() == old.resolve()]
    pdf = old / "paper" / "main.pdf"
    return "\n\n".join([
        f"=== THE LAST RUN: {old} ===",
        f"Its paper: {pdf if pdf.is_file() else '(no PDF)'}",
        "--- the paper's text ---\n" + (paper or "(the run wrote no paper)"),
        "--- its review ---\n" + (S.render({"reviews": reviews[-1:]}, ["reviews"]).split("\n", 1)[-1]
                                  if reviews else "(not reviewed)")])
