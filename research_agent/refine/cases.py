"""
WHAT:  The cases a role's prompt is refined on: calls it made in past runs,
       read back from the runs' folders -- what it was sent, what it
       answered, what code sent back, and what happened next.
WHY:   Refinement learns from real work, and nothing new has to be
       recorded: every call is kept as a session in <run>/rb, every change to
       the research state in <run>/state/history, every word in the chat.
CONCEPT: A case is one call (a session): its first turn's prompt is the brief
       to replay; later turns that start "Your answer could not be used" are
       the code checks it failed. The aftermath -- the user's next words,
       approvals, revise requests, results and verdicts -- is the only
       verdict on it there is: refinement has no ground truth.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from research_agent import chat
from research_agent import state as S

#: Role -> (session name prefix, the schema its calls answer with, stage).
ROLES = {
    "designer": ("designer-", "DesignOutput", "bootstrap"),
    "orchestrator": ("orchestrator-", "OrchestratorOutput", "bootstrap"),
    "exec_orchestrator": ("orchestrator-", "ExecDecision", "execute"),
}
RETRY = "Your answer could not be used"
#: Characters of aftermath kept per case.
AFTERMATH_CHARS = 2500
#: Words in what happened next that say the call went wrong.
TROUBLE = re.compile(r"revis|not ready|again|wrong|fail|stuck|loop|instead|blocked|could not",
                     re.I)


@dataclass
class Case:
    id: str
    """<run folder name>/<session>"""
    run: str
    session: str
    role: str
    schema: str
    at: str
    instructions: str
    """The standing instructions the call was made with."""
    brief: str
    """Its first message: what a replay sends."""
    reply: str
    """Its last answer, as recorded."""
    problems: list[str] = field(default_factory=list)
    """What the code checks sent back, attempt by attempt."""
    aftermath: str = ""

    @property
    def score(self) -> int:
        """How much there is to learn: retries, then trouble afterwards."""
        return 3 * len(self.problems) + len(TROUBLE.findall(self.aftermath))

    def to_dict(self) -> dict:
        return asdict(self)


def collect(run_dirs, role: str, limit: int = 4) -> list[Case]:
    """The `limit` most informative cases of `role` in these runs: calls that
    were sent back, or that trouble followed; then the newest."""
    if role not in ROLES:
        raise ValueError(f"No role {role!r} to refine; they are {', '.join(ROLES)}.")
    found = [c for run in run_dirs for c in cases(Path(run).expanduser(), role)]
    found.sort(key=lambda c: (c.score, c.at), reverse=True)
    return found[:limit]


def cases(run: Path, role: str) -> list[Case]:
    """Every recorded call of `role` in one run, oldest first."""
    prefix, schema, _ = ROLES[role]
    root = run / "rb" / "sessions"
    out = []
    for folder in sorted(root.glob(f"{prefix}*")) if root.is_dir() else []:
        try:
            session = json.loads((folder / "session.json").read_text())
        except (OSError, ValueError):
            continue
        turns = session.get("turns") or []
        if not turns or turns[0].get("schema") != schema:
            continue
        problems = [_problems(t.get("prompt") or "") for t in turns[1:]
                    if (t.get("prompt") or "").startswith(RETRY)]
        out.append(Case(id=f"{run.name}/{folder.name}", run=str(run), session=folder.name,
                        role=role, schema=schema, at=turns[0].get("at") or "",
                        instructions=session.get("instructions") or "",
                        brief=turns[0].get("prompt") or "", reply=turns[-1].get("reply") or "",
                        problems=[p for p in problems if p]))
    out.sort(key=lambda c: c.at)
    for i, case in enumerate(out):
        following = out[i + 1] if i + 1 < len(out) else None
        case.aftermath = aftermath(run, case.at, following.at if following else "",
                                   session=case.session,
                                   next_session=following.session if following else "")
    return out


def _problems(prompt: str) -> str:
    lines = [ln[2:] for ln in prompt.splitlines() if ln.startswith("- ")]
    return "; ".join(lines)


# ---- what happened next ----------------------------------------------------------------


def aftermath(run: Path, since: str, until: str = "", *, session: str = "",
              next_session: str = "") -> str:
    """What followed a call, up to the role's next call: the user's words and
    the changes to the research state that matter -- approvals, decisions,
    results and verdicts."""
    lines = []
    for m in chat.transcript(run):
        if m.get("at") and m["at"] >= since and (not until or m["at"] < until) \
                and m["role"] == "user":
            lines.append((m["at"], f"the user said: {m['text'][:400]}"))
    entries = history(run)
    start = _boundary(entries, session, since, after=True)
    end = _boundary(entries, next_session, until, after=False) if (next_session or until) \
        else len(entries)
    for entry in entries[start:end]:
        for op in entry.get("ops") or []:
            said = _op_line(op, entry.get("by", ""))
            if said:
                lines.append((entry["at"], said))
    lines.sort(key=lambda x: x[0])
    text = "\n".join(f"- {line}" for _, line in lines)
    return text[:AFTERMATH_CHARS] + ("\n(more left out)" if len(text) > AFTERMATH_CHARS else "")


def _boundary(entries: list[dict], session: str, at: str, *, after: bool) -> int:
    """Where a call sits in the history: the index of its own entry (after:
    the one past it) -- the entry tagged with its session, or with a result
    naming it; for older runs, the first entry made after it started."""
    for i, entry in enumerate(entries):
        if session and (entry.get("session") == session or any(
                (op.get("value") or {}).get("session") == session
                for op in entry.get("ops") or [] if isinstance(op.get("value"), dict))):
            return i + 1 if after else i
    if not at:
        return len(entries)
    return next((i for i, e in enumerate(entries) if e.get("at", "") > at), len(entries))


def _op_line(op: dict, by: str) -> str:
    section, value = op.get("section"), op.get("value") or {}
    if op.get("op") == "approve":
        return f"{by or 'the user'} approved the {section}"
    if section == "decisions" and op.get("op") == "add":
        return f"decision by {value.get('by', by)}: {str(value.get('what', ''))[:300]}"
    if section == "results" and op.get("op") == "add":
        return f"{value.get('node', by)}: {str(value.get('summary', ''))[:300]}"
    return ""


# ---- the state a call saw --------------------------------------------------------------


def history(run: Path) -> list[dict]:
    folder = run / "state" / "history"
    out = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            out.append(json.loads(path.read_text()))
        except ValueError:
            continue
    return out


def state_at(run: Path, at: str, session: str = "") -> tuple[dict, bool]:
    """The research state as it was when a call was made at `at` (by
    `session`), rebuilt from the history; (state, exact). Not exact when the
    history cannot be replayed -- a run carried from another starts from a
    copy, not ops -- and then the run's current state stands in."""
    entries = history(run)
    st = S.new()
    for entry in entries[:_boundary(entries, session, at, after=False)]:
        try:
            st, _ = S.apply(st, entry.get("ops") or [], by=entry.get("by", ""))
        except (S.StateError, ValueError, KeyError, TypeError):
            return S.Store(run).read(), False
    return st, True
