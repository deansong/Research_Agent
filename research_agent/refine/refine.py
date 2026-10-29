"""
WHAT:  Refining one role's prompt from past runs, end to end: collect cases,
       replay them with the current instructions, propose rewrites (INV and
       DOM), replay those, compose, replay that, judge blindly, write the
       lessons, adopt the winner, report.
WHY:   The method of google-research/harness2 (Propose-Probe-Compose with an
       experience library), on research_agent's own backends: no Vertex, no
       OpenCode, no benchmark -- the cases are the role's own past calls.
CONCEPT: The base wins unless a candidate wins more cases than it and than
       every other. A winner is written to your prompts folder
       (prompt_files.user_dir()), the file it replaces kept beside it as
       <role>.md.<time>.bak, so every later run uses it and it is easy to undo.
       Everything is kept in <runs root>/_refine/<time>-<role>/.
"""

from __future__ import annotations

import difflib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from research_agent import prompt_files
from research_agent import run as run_base
from research_agent.bootstrap import config as boot_config
from research_agent.refine import cases as C
from research_agent.refine import experience, propose, replay, select

#: The prompt file each role's instructions are in.
PROMPTS = {"designer": "roles/bootstrap/designer", "orchestrator": "roles/bootstrap/orchestrator",
           "exec_orchestrator": "roles/execute/orchestrator"}
LABELS = ("base", "inv", "dom", "composed")


def estimate(n_cases: int, k: int) -> int:
    """Model calls a refinement makes at most: four sets of replays, two
    proposers, a composer, a judge per case, the lessons."""
    return 4 * n_cases * k + 3 + n_cases + 1


@dataclass
class Report:
    role: str
    folder: Path
    cases: list = field(default_factory=list)
    texts: dict = field(default_factory=dict)
    """Label -> its instructions (the candidates that were made)."""
    notes: dict = field(default_factory=dict)
    picks: list = field(default_factory=list)
    wins: dict = field(default_factory=dict)
    winner: str = "base"
    adopted: str = ""
    """The file written, "" when the base stays."""
    backup: str = ""
    lessons: dict = field(default_factory=dict)
    calls: int = 0


def refine(role: str, run_dirs, *, n_cases: int = 4, k: int = 1, model: str = "",
           refiner: str = "", budget: int = 40, root: Path | None = None, adopt: bool = True,
           say=print, listener=None) -> Report:
    """Refine `role`'s instructions on its calls in `run_dirs`."""
    if role not in PROMPTS:
        raise ValueError(f"No role {role!r} to refine; they are {', '.join(PROMPTS)}.")
    found = C.collect(run_dirs, role, n_cases)
    if not found:
        raise ValueError(f"No recorded {role} calls in {', '.join(map(str, run_dirs))}.")
    calls = estimate(len(found), k)
    if calls > budget:
        raise ValueError(f"This refinement would make up to {calls} model calls; the budget is "
                         f"{budget} (--budget, or fewer --cases).")
    root = Path(root or Path(found[0].run).parent)
    stem = root / "_refine" / f"{datetime.now():%Y%m%d-%H%M%S}-{role}"
    folder, n = stem, 1
    while folder.exists():
        n += 1
        folder = stem.with_name(f"{stem.name}-{n}")
    folder.mkdir(parents=True)
    report = Report(role=role, folder=folder, cases=[c.id for c in found])
    say(f"Refining the {role}'s instructions on {len(found)} cases (up to {calls} calls): "
        f"{', '.join(c.id for c in found)}")
    run = run_base.Run(folder, {"refiner": _refiner(refiner)}, quiet=True, listener=listener)
    base = prompt_files.text(PROMPTS[role])
    report.texts["base"] = base
    outputs: dict[str, list] = {c.id: [] for c in found}

    def probe(label: str, text: str) -> None:
        for case in found:
            for _ in range(k):
                out = replay.replay(case, text, label=label, home=folder / "replays",
                                    model=model, listener=listener)
                outputs[case.id].append(out)
                state = ("ok" if out.passed and not out.first_problems else
                         "ok after a retry" if out.passed else "no answer")
                say(f"  . replay {label} on {case.id}: {state}")

    probe("base", base)
    lessons = experience.lessons_for(role)
    proposals = {}
    for dim in ("inv", "dom"):
        p = propose.propose(run, dim, role=role, base=base, cases=found,
                            baseline={c: [o for o in outputs[c] if o.label == "base"]
                                      for c in outputs}, lessons=lessons)
        report.notes[dim] = [n.model_dump() for n in p.notes]
        if p.keep_base or p.instructions.strip() == base.strip():
            say(f"  . {dim}: keep the base")
            continue
        proposals[dim] = p
        report.texts[dim] = p.instructions.strip()
        probe(dim, report.texts[dim])
    if proposals:
        composed = propose.compose(run, role=role, base=base, proposals=proposals, cases=found,
                                   outputs=outputs)
        report.notes["composed"] = [n.model_dump() for n in composed.notes]
        text = composed.instructions.strip()
        if not composed.keep_base and text and text not in report.texts.values():
            report.texts["composed"] = text
            probe("composed", text)
    for case in found:
        shown = _one_each(outputs[case.id])
        pick = select.judge(run, case, shown)
        report.picks.append({"case": pick.case, "winner": pick.winner, "why": pick.why,
                             "shown": pick.shown})
        say(f"  . judge {case.id}: {pick.winner or 'none'}")
    picks = [select.Pick(p["case"], p["winner"]) for p in report.picks]
    report.winner, report.wins = select.tally(picks)
    report.lessons = experience.write(run, role=role, record=_record(report, outputs),
                                      folder=folder).model_dump()
    if report.winner != "base" and adopt:
        report.adopted, report.backup = adopt_text(role, report.texts[report.winner])
    report.calls = calls
    write_report(report, outputs)
    say(f"Done: {report.winner} wins ({report.wins}); "
        + (f"adopted into {report.adopted}" if report.adopted else "the instructions stay")
        + f". Report: {folder / 'report.md'}")
    return report


def _refiner(spec: str):
    if spec:
        return boot_config.parse_role(f"refiner={spec}")[1]
    return boot_config.load(None).roles["refiner"]


def _one_each(outputs: list) -> list:
    """The judge sees one answer per instructions: the first that passed."""
    chosen = {}
    for o in outputs:
        if o.label not in chosen or (o.passed and not chosen[o.label].passed):
            chosen[o.label] = o
    return list(chosen.values())


def adopt_text(role: str, text: str) -> tuple[str, str]:
    """Write `text` as the role's instructions in your prompts folder; keep
    the file it replaces beside it. Returns (file, backup or "")."""
    target = prompt_files.user_dir() / f"{PROMPTS[role]}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = ""
    if target.exists():
        backup = str(target.with_name(f"{target.name}.{datetime.now():%Y%m%d-%H%M%S}.bak"))
        shutil.copy(target, backup)
    target.write_text(f"<!-- Adopted by research_agent refine on {datetime.now():%Y-%m-%d %H:%M}. "
                      f"Delete this file to go back to the default. -->\n{text.strip()}\n",
                      encoding="utf-8")
    return str(target), backup


def _record(report: Report, outputs: dict) -> str:
    parts = [f"=== BASE INSTRUCTIONS ===\n{report.texts['base']}"]
    for label in LABELS[1:]:
        if label in report.texts:
            parts.append(f"=== {label.upper()} ===\n{report.texts[label]}\nnotes: "
                         + json.dumps(report.notes.get(label, []), ensure_ascii=False))
    for case_id, outs in outputs.items():
        parts.append(f"=== REPLAYS OF {case_id} ===\n" + "\n".join(
            f"- {o.label}: " + ("passed" if o.passed and not o.first_problems else
                                f"passed after a retry ({'; '.join(o.first_problems)[:200]})"
                                if o.passed else f"no answer ({o.error[:200]})")
            + f"\n  {propose.clip(o.answer, 1500)}" for o in outs))
    parts.append("=== THE BLIND CHOICE ===\n" + "\n".join(
        f"- {p['case']}: {p['winner'] or 'none'} -- {p['why']}" for p in report.picks)
        + f"\nwinner: {report.winner} ({report.wins})")
    return "\n\n".join(parts)


def write_report(report: Report, outputs: dict) -> None:
    folder = report.folder
    data = {k: (str(v) if isinstance(v, Path) else v) for k, v in report.__dict__.items()}
    data["outputs"] = {c: [o.to_dict() for o in outs] for c, outs in outputs.items()}
    (folder / "report.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    lines = [f"# Refining the {report.role}'s instructions", "",
             f"Cases: {', '.join(report.cases)}", "",
             f"**Winner: {report.winner}** -- case wins {report.wins}. "
             + (f"Adopted into `{report.adopted}`"
                + (f" (the old file kept as `{report.backup}`)" if report.backup else "")
                if report.adopted else "The instructions stay as they were."), ""]
    for label in LABELS[1:]:
        if label not in report.texts:
            continue
        diff = difflib.unified_diff(report.texts["base"].splitlines(),
                                    report.texts[label].splitlines(), "base", label, lineterm="")
        lines += [f"## {label}", "", "```diff", *diff, "```", ""]
        for n in report.notes.get(label, []):
            lines.append(f"- {n['change']} -- because {n['motivated_by']}")
        lines.append("")
    lines += ["## Blind choice per case", ""]
    for p in report.picks:
        lines.append(f"- {p['case']}: **{p['winner'] or 'none'}** -- {p['why']}")
    lines += ["", "## Replays", ""]
    for case_id, outs in outputs.items():
        lines.append(f"- {case_id}: " + ", ".join(
            f"{o.label} {'ok' if o.passed and not o.first_problems else 'retry' if o.passed else 'failed'}"
            for o in outs))
    les = report.lessons or {}
    lines += ["", "## What each edit did", ""]
    lines += [f"- {e['change']}: {e['observed']} -- **{e['verdict']}**" for e in les.get("edits", [])]
    if les.get("lesson"):
        lines += ["", "## Lesson (kept in the library)", "", f"- cue: {les['cue']}",
                  f"- lesson: {les['lesson']}", f"- evidence: {les['evidence']}",
                  f"- confidence: {les['confidence']}"]
    (folder / "report.md").write_text("\n".join(lines) + "\n")
