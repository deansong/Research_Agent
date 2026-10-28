"""
WHAT:  The execution stage's configuration: which provider each role runs on,
       the skills folder, and the limits code holds (confirmations, attempts,
       wall and GPU hours).
WHY:   Same as the bootstrap's: choices you can read and edit, in a file.
CONCEPT: Three layers, later ones winning:

    research_agent/execute/config.toml   the defaults, commented
    <run>/execute.json                   what a run's execution started with
    --role / --wall-hours / ...          one start of the CLI

The discussor is not here: it is the bootstrap's, continuing its conversation.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from research_agent.bootstrap.config import REPOSITORY, RoleConfig, backup_problems, role_fields

#: Roles the execution stage runs. Step owners are workers (code, data, runs)
#: and writers (the report), by the role the design gives them.
ROLES = ("orchestrator", "worker", "writer", "validator", "reviewer")
OWNERS = ("worker", "writer")
#: A role of earlier versions, run as a worker: the two did not differ.
RETIRED = {"runner": "worker"}
CONFIRM = ("gated", "all", "none")

DEFAULT_FILE = Path(__file__).with_name("config.toml")
LIMITS = ("confirm", "max_attempts", "wall_hours", "gpu_hours", "poll_seconds", "max_decisions")


@dataclass(frozen=True)
class ExecConfig:
    roles: dict[str, RoleConfig]
    skills_dir: Path | None
    reviewer_skills: tuple = ("peer-review", "scientific-critical-thinking")
    node_skills: dict = field(default_factory=dict)
    """Graph node -> its skills, over the design's: None, "auto" or names."""
    confirm: str = "gated"
    max_attempts: int = 3
    wall_hours: float = 0.0
    gpu_hours: float = 0.0
    poll_seconds: float = 30.0
    max_decisions: int = 30

    def describe(self) -> str:
        roles = ", ".join(f"{r}={c}" for r, c in self.roles.items())
        limits = ", ".join(f"{k}={getattr(self, k)}" for k in LIMITS)
        return f"Roles: {roles}\nLimits: {limits}\nSkills: {self.skills_dir or 'no folder'}"

    def to_dict(self) -> dict:
        # Every field, empty ones too: a role given without a model must not
        # get the default's model back when the file is read again.
        return {"roles": {r: asdict(c) for r, c in self.roles.items()},
                "skills": {"dir": str(self.skills_dir) if self.skills_dir else None,
                           "reviewer": list(self.reviewer_skills),
                           "nodes": {n: c if c is not None else "none"
                                     for n, c in self.node_skills.items()}},
                "execute": {k: getattr(self, k) for k in LIMITS}}


def defaults(path: Path = DEFAULT_FILE) -> ExecConfig:
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    return _merge(ExecConfig({}, None), data, base=REPOSITORY)


def load(run_dir: str | Path | None = None, *, roles=(), limits: dict | None = None,
         base: Path = DEFAULT_FILE) -> ExecConfig:
    """The defaults, then <run>/execute.json, then the command line. Raises
    ValueError for anything that is not there."""
    cfg = defaults(base)
    path = Path(run_dir) / "execute.json" if run_dir else None
    if path and path.exists():
        cfg = _merge(cfg, json.loads(path.read_text(encoding="utf-8")), base=Path(run_dir))
    for text in roles:
        role, spec = parse_role(text)
        for name in ROLES if role == "all" else (role,):
            cfg = replace(cfg, roles={**cfg.roles, name: spec})
    cfg = replace(cfg, **{k: v for k, v in (limits or {}).items() if v is not None})
    problems = check(cfg)
    if problems:
        raise ValueError("The configuration has problems:\n" + "\n".join(f"- {p}" for p in problems))
    return cfg


def save(run_dir: str | Path, cfg: ExecConfig) -> None:
    (Path(run_dir) / "execute.json").write_text(json.dumps(cfg.to_dict(), indent=2) + "\n")


def check(cfg: ExecConfig) -> list[str]:
    problems = [f"no role {r!r} (the roles are {', '.join(ROLES)})" for r in cfg.roles
                if r not in ROLES]
    problems += [f"role {r!r} is not configured" for r in ROLES if r not in cfg.roles]
    problems += backup_problems(cfg.roles)
    if cfg.confirm not in CONFIRM:
        problems.append(f"confirm is {cfg.confirm!r}; it is one of {', '.join(CONFIRM)}")
    if cfg.max_attempts < 1:
        problems.append("max_attempts must be at least 1")
    if cfg.wall_hours < 0 or cfg.gpu_hours < 0:
        problems.append("the hour limits cannot be negative (0 = no limit)")
    return problems


def parse_role(text: str) -> tuple[str, RoleConfig]:
    """'worker=claude_code:opus' -> ("worker", RoleConfig(...)); 'all=...'."""
    role, sep, spec = text.partition("=")
    role = role.strip()
    if not sep or not spec.strip():
        raise ValueError(f"--role takes role=provider[:model[:effort]], not {text!r}.")
    if role != "all" and role not in ROLES:
        raise ValueError(f"No role {role!r}; the roles are {', '.join(ROLES)} (or all).")
    provider, model, effort = (spec.split(":") + ["", ""])[:3]
    return role, RoleConfig(provider.strip(), model.strip() or None, effort.strip() or None)


def _merge(cfg: ExecConfig, data: dict, *, base: Path) -> ExecConfig:
    roles = dict(cfg.roles)
    for role, spec in (data.get("roles") or {}).items():
        if role in RETIRED:
            continue
        known = roles.get(role)
        fields = role_fields(spec)
        roles[role] = replace(known, **fields) if known else RoleConfig(**fields)
    skills_dir = cfg.skills_dir
    folder = (data.get("skills") or {}).get("dir", ...)
    if folder is not ...:
        skills_dir = (base / Path(folder).expanduser()).resolve() if folder else None
    reviewer = (data.get("skills") or {}).get("reviewer", cfg.reviewer_skills)
    nodes = {**cfg.node_skills, **{n: _choice(c) for n, c in
                                   ((data.get("skills") or {}).get("nodes") or {}).items()}}
    limits = {k: v for k, v in (data.get("execute") or {}).items() if k in LIMITS}
    return replace(cfg, roles=roles, skills_dir=skills_dir,
                   reviewer_skills=tuple(reviewer or ()), node_skills=nodes, **limits)


def _choice(value):
    """"none" / "auto" / a list of names, as kept in execute.json."""
    if value in (None, "", "none", [], ()):
        return None
    return "auto" if value == "auto" else [value] if isinstance(value, str) else list(value)
