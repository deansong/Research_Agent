"""
WHAT:  Replaying a case: the call a role made in a past run, made again with
       other instructions -- what it answers, and whether the role's own code
       checks pass.
WHY:   That is the probe of Propose-Probe-Compose: a candidate prompt is
       judged by what it makes the model do on real work, not by how it reads.
CONCEPT: The replay sends the recorded brief, as it was, through a scratch
       Run under the refinement's folder -- its own rb home, read-only, never
       the past run's state. The check is the role's real one, given the
       state the call saw (cases.state_at).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from research_agent import run as run_base
from research_agent.bootstrap import config as boot_config
from research_agent.refine.cases import ROLES, Case, state_at


@dataclass
class Output:
    case: str
    label: str
    """Which instructions: base, inv, dom, composed."""
    answer: str = ""
    """The answer, as JSON text ("" when there was none)."""
    first_problems: list[str] = field(default_factory=list)
    """What the code checks found in its first answer."""
    attempts: int = 0
    error: str = ""
    """Set when no usable answer came within the attempts."""

    @property
    def passed(self) -> bool:
        return bool(self.answer) and not self.error

    def to_dict(self) -> dict:
        return asdict(self)


def schema_of(role: str):
    if role == "designer":
        from research_agent.bootstrap.schemas import DesignOutput
        return DesignOutput
    if role == "orchestrator":
        from research_agent.bootstrap.schemas import OrchestratorOutput
        return OrchestratorOutput
    from research_agent.execute.schemas import ExecDecision
    return ExecDecision


def model_of(case: Case, override: str = ""):
    """The model the case's role ran on in its run, or `override`
    ("provider[:model[:effort]]")."""
    if override:
        return boot_config.parse_role(f"designer={override}")[1]
    run = Path(case.run)
    if ROLES[case.role][2] == "execute":
        from research_agent.execute import config as exec_config
        return exec_config.load(run).roles["orchestrator"]
    return boot_config.load(run).roles[case.role]


def check_of(case: Case):
    """The role's code check, on the state the call saw."""
    st, _ = state_at(Path(case.run), case.at, case.session)
    if case.role == "designer":
        from research_agent.bootstrap.nodes import design_check
        known = {name for name, _ in run_base.Run(Path(case.run), {}, skills_dir=_skills_dir(),
                                                  quiet=True).catalogue()}
        return design_check(st, known)
    if case.role == "orchestrator":
        from research_agent.bootstrap.nodes import orchestrator_check
        return orchestrator_check
    from research_agent.execute import config as exec_config
    from research_agent.execute.nodes import decision_check
    return decision_check(st, exec_config.load(Path(case.run)).max_attempts, case.run)


def _skills_dir():
    return boot_config.defaults().skills_dir


def replay(case: Case, instructions: str, *, label: str, home: Path, model: str = "",
           listener=None, quiet: bool = True) -> Output:
    """One replay of `case` with `instructions`."""
    out = Output(case=case.id, label=label)
    check = check_of(case)
    seen: list[list[str]] = []

    def checked(answer) -> list[str]:
        problems = check(answer)
        seen.append(list(problems))
        return problems

    role = case.role
    folder = Path(home) / label / case.id.replace("/", "__")
    folder.mkdir(parents=True, exist_ok=True)
    run = run_base.Run(folder, {role: model_of(case, model)}, quiet=quiet, listener=listener)
    try:
        answer, _ = run.call(role, brief=case.brief, schema=schema_of(role), skills=None,
                             instructions=instructions, access="read_only", check=checked,
                             label=f"replay {label}: {case.id}")
        out.answer = answer.model_dump_json(indent=1)
    except Exception as exc:  # noqa: BLE001 -- a failed replay is a result, not a crash
        out.error = str(exc)[:1000]
    out.attempts = len(seen)
    out.first_problems = seen[0] if seen else []
    return out
