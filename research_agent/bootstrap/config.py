"""
WHAT:  The bootstrap's configuration: which provider, model and effort each
       role runs on, and which skills each node is given.
WHY:   The discussor talks for hours and should be the best writer; the
       researcher needs web and the research tools; the designer should think
       hard. Those choices belong in a file you can read and edit, not in code.
CONCEPT: Three layers, later ones winning:

    research_agent/bootstrap/config.toml   the defaults, commented
    <run>/config.json                      what a run started with (edit it to
                                           change that run; read on --resume)
    --role / --skills / --skills-dir       one start of the CLI

A skills choice is None (no skills), "auto" (every skill's name, description
and path; the model reads what it needs) or a list of skill names (in full).
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

ROLES = ("discussor", "orchestrator", "researcher", "designer")
#: What a skills choice is keyed by: each role.
NODES = ROLES
#: Roles and skills keys of earlier versions, left out when an old run's
#: config.json is read: the designer now does the planner's work.
RETIRED = ("planner", "planner_hypothesize", "planner_plan")

DEFAULT_FILE = Path(__file__).with_name("config.toml")
#: Where a relative skills folder is taken from: the repository root.
REPOSITORY = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class RoleConfig:
    provider: str
    model: str | None = None
    effort: str | None = None
    options: dict = field(default_factory=dict)
    backup: tuple[str, ...] = ()
    """What the role moves to, in order, when this model is out of credits
    or cannot be reached: "provider[:model[:effort]]" each (see run.py)."""

    def __str__(self) -> str:
        return ":".join(x for x in (self.provider, self.model or "", self.effort or "") if x)

    def backups(self) -> list["RoleConfig"]:
        """The backups as RoleConfigs (options: none of the main model's)."""
        return [spec_of(text) for text in self.backup]


def spec_of(text: str) -> RoleConfig:
    """'codex:gpt-6-sol:high' -> RoleConfig."""
    provider, model, effort = (str(text).strip().split(":") + ["", ""])[:3]
    if not provider.strip():
        raise ValueError(f"{text!r} names no provider; write provider[:model[:effort]].")
    return RoleConfig(provider.strip(), model.strip() or None, effort.strip() or None)


def role_fields(spec: dict) -> dict:
    """The RoleConfig fields of a role as a config file has it."""
    fields = {k: v for k, v in spec.items() if k in ("provider", "model", "effort", "options",
                                                    "backup")}
    if "backup" in fields:
        backup = fields["backup"] or ()
        fields["backup"] = tuple([backup] if isinstance(backup, str) else backup)
    return fields


@dataclass(frozen=True)
class BootstrapConfig:
    roles: dict[str, RoleConfig]
    skills: dict[str, None | str | list[str]]
    skills_dir: Path | None
    sources: tuple[str, ...] = ("openalex", "s2")
    """The literature sources the research skill may search (SOURCES)."""

    def describe(self) -> str:
        """Two readable lines: roles, then skills."""
        roles = ", ".join(f"{r}={c}" for r, c in self.roles.items())
        skills = ", ".join(f"{n}={_word(c)}" for n, c in self.skills.items())
        return f"Roles: {roles}\nSkills ({self.skills_dir or 'no folder'}): {skills}"

    def to_dict(self) -> dict:
        # Every field, empty ones too: a role given without a model must not
        # get the default's model back when the file is read again.
        return {"roles": {r: {"provider": c.provider, "model": c.model, "effort": c.effort,
                              "options": c.options, "backup": list(c.backup)}
                          for r, c in self.roles.items()},
                "skills": {"dir": str(self.skills_dir) if self.skills_dir else None,
                           **{n: c if c is not None else "none" for n, c in self.skills.items()}},
                "research": {"sources": list(self.sources)}}


#: The research skill's literature sources: name, what it is, the key it needs.
SOURCES = (
    ("openalex", "OpenAlex", "every field and publisher, ~250M works", ""),
    ("s2", "Semantic Scholar", "computer science, citation graph", "SEMANTIC_SCHOLAR_API_KEY"),
    ("pubmed", "PubMed", "biomedicine and clinical work", ""),
    ("europepmc", "Europe PMC", "life sciences, preprints, open full text", ""),
    ("scholar", "Google Scholar", "through SerpAPI; each search costs a credit", "SERPAPI_API_KEY"),
)

#: The file in the run folder the research skill reads the choice from.
SOURCES_FILE = ".research_sources"


def write_sources(run_dir: str | Path, sources) -> None:
    """Tell the research skill's tool (skills/research/research_tools.py)
    which sources it may search: it looks for this file from its working
    folder upwards, so a change applies to the researcher's next command."""
    (Path(run_dir) / SOURCES_FILE).write_text(json.dumps({"sources": list(sources)}) + "\n")


# ---- reading it --------------------------------------------------------------------------


def defaults(path: Path = DEFAULT_FILE) -> BootstrapConfig:
    """The defaults file."""
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    return _merge(BootstrapConfig({}, {}, None), data, base=REPOSITORY)


def load(run_dir: str | Path | None = None, *, roles=(), skills=(), skills_dir=None,
         base: Path = DEFAULT_FILE) -> BootstrapConfig:
    """The configuration for a run: the defaults, then the run's config.json,
    then the command line. Raises ValueError for anything that is not there
    (a role, a node, a skill, the skills folder)."""
    cfg = defaults(base)
    path = Path(run_dir) / "config.json" if run_dir else None
    if path and path.exists():
        cfg = _merge(cfg, json.loads(path.read_text(encoding="utf-8")), base=Path(run_dir))
    for text in roles:
        role, spec = parse_role(text)
        for name in ROLES if role == "all" else (role,):
            cfg = replace(cfg, roles={**cfg.roles, name: spec})
    if skills_dir:
        cfg = replace(cfg, skills_dir=Path(skills_dir).expanduser().resolve())
    for text in skills:
        node, choice = parse_skills(text)
        cfg = replace(cfg, skills={**cfg.skills, node: choice})
    problems = check(cfg)
    if problems:
        raise ValueError("The configuration has problems:\n" + "\n".join(f"- {p}" for p in problems))
    return cfg


def save(run_dir: str | Path, cfg: BootstrapConfig) -> None:
    """What a run starts with, so --resume runs the same way."""
    (Path(run_dir) / "config.json").write_text(json.dumps(cfg.to_dict(), indent=2) + "\n")
    write_sources(run_dir, cfg.sources)


def check(cfg: BootstrapConfig) -> list[str]:
    """What is wrong with a configuration, in words."""
    problems = [f"no role {r!r} (the roles are {', '.join(ROLES)})" for r in cfg.roles
                if r not in ROLES]
    problems += [f"role {r!r} is not configured" for r in ROLES if r not in cfg.roles]
    problems += backup_problems(cfg.roles)
    known = {name for name, *_ in SOURCES}
    problems += [f"research: no source {s!r} (they are {', '.join(sorted(known))})"
                 for s in cfg.sources if s not in known]
    problems += [f"no node {n!r} for skills (they are {', '.join(NODES)})" for n in cfg.skills
                 if n not in NODES]
    chosen = {n: c for n, c in cfg.skills.items() if c is not None}
    if chosen and not (cfg.skills_dir and cfg.skills_dir.is_dir()):
        problems.append(f"skills are chosen but there is no skills folder at {cfg.skills_dir}")
    elif chosen:
        import research_backend as RB

        known = set(RB.list_skills(cfg.skills_dir).names)
        for node, choice in chosen.items():
            if isinstance(choice, list):
                problems += [f"{node}: no skill called {s!r} in {cfg.skills_dir}"
                             for s in choice if s not in known]
    return problems


def backup_problems(roles: dict) -> list[str]:
    """Backups that name no provider research_backend has."""
    import research_backend as RB

    known = set(RB.list_providers())
    problems = []
    for role, c in roles.items():
        for text in c.backup:
            try:
                provider = spec_of(text).provider
            except ValueError as exc:
                problems.append(f"{role}: backup {exc}")
                continue
            if provider not in known:
                problems.append(f"{role}: backup {text!r}: no provider {provider!r}")
    return problems


# ---- the command line's forms ----------------------------------------------------------


def parse_role(text: str) -> tuple[str, RoleConfig]:
    """'designer=codex:gpt-5.6-sol:high' -> ("designer", RoleConfig(...));
    'all=...' sets every role."""
    role, sep, spec = text.partition("=")
    role = role.strip()
    if not sep or not spec.strip():
        raise ValueError(f"--role takes role=provider[:model[:effort]], not {text!r}.")
    if role != "all" and role not in ROLES:
        raise ValueError(f"No role {role!r}; the roles are {', '.join(ROLES)} (or all).")
    provider, model, effort = (spec.split(":") + ["", ""])[:3]
    return role, RoleConfig(provider.strip(), model.strip() or None, effort.strip() or None)


def parse_skills(text: str) -> tuple[str, None | str | list[str]]:
    """'designer=experiment-design,ml-statistics' -> a list;
    'discussor=auto'; 'designer=none'."""
    node, sep, spec = text.partition("=")
    node = node.strip()
    if not sep:
        raise ValueError(f"--skills takes node=none|auto|skill,skill, not {text!r}.")
    if node not in NODES:
        raise ValueError(f"No node {node!r}; the nodes are {', '.join(NODES)}.")
    return node, _choice([s.strip() for s in spec.split(",") if s.strip()]
                         if "," in spec else spec.strip())


# ---- helpers ------------------------------------------------------------------------------


def _merge(cfg: BootstrapConfig, data: dict, *, base: Path) -> BootstrapConfig:
    roles = dict(cfg.roles)
    for role, spec in (data.get("roles") or {}).items():
        if role in RETIRED:
            continue
        known = roles.get(role)
        fields = role_fields(spec)
        roles[role] = replace(known, **fields) if known else RoleConfig(**fields)
    skills = dict(cfg.skills)
    skills_dir = cfg.skills_dir
    for key, value in (data.get("skills") or {}).items():
        if key in RETIRED:
            continue
        if key == "dir":
            skills_dir = (base / Path(value).expanduser()).resolve() if value else None
        else:
            skills[key] = _choice(value)
    sources = (data.get("research") or {}).get("sources")
    return BootstrapConfig(roles, skills, skills_dir,
                           tuple(sources) if sources is not None else cfg.sources)


def _choice(value) -> None | str | list[str]:
    if value in (None, "", "none", [], ()):
        return None
    if value == "auto":
        return "auto"
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _word(choice) -> str:
    if choice is None:
        return "none"
    return choice if isinstance(choice, str) else "+".join(choice)
