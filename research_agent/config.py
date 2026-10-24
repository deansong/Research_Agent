"""
WHAT:  Which provider, model and effort each role runs on.
WHY:   The discussor talks for hours and should be the best writer; the
       researcher needs web and tools; the planner should think hard. One
       table, overridable per run, says so.
CONCEPT: Defaults here, then <run>/config.json, then --role flags:

    {"roles": {"planner": {"provider": "codex", "model": "gpt-5.6-sol",
                           "effort": "high", "options": {"timeout": 600}}}}

    --role planner=codex:gpt-5.6-sol:high      provider[:model[:effort]]
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

ROLES = ("discussor", "orchestrator", "researcher", "planner", "designer")


@dataclass(frozen=True)
class RoleConfig:
    provider: str
    model: str | None = None
    effort: str | None = None
    options: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return ":".join(x for x in (self.provider, self.model or "", self.effort or "") if x) \
            or self.provider


DEFAULTS: dict[str, RoleConfig] = {
    "discussor": RoleConfig("claude_code", "opus"),
    "orchestrator": RoleConfig("codex"),
    "researcher": RoleConfig("antigravity"),
    "planner": RoleConfig("codex", effort="high"),
    "designer": RoleConfig("codex"),
}


def parse(text: str) -> tuple[str, RoleConfig]:
    """'planner=codex:gpt-5.6-sol:high' -> ("planner", RoleConfig(...)).
    'all=fake' sets every role."""
    role, sep, spec = text.partition("=")
    role = role.strip()
    if not sep or not spec.strip():
        raise ValueError(f"--role takes role=provider[:model[:effort]], not {text!r}.")
    if role != "all" and role not in ROLES:
        raise ValueError(f"No role {role!r}; the roles are {', '.join(ROLES)} (or all).")
    provider, model, effort = (spec.split(":") + ["", ""])[:3]
    return role, RoleConfig(provider.strip(), model.strip() or None, effort.strip() or None)


def load(run_dir: str | Path | None = None, flags: list[str] | tuple = ()) -> dict[str, RoleConfig]:
    """The roles for a run: defaults, then the run's config.json, then flags."""
    roles = dict(DEFAULTS)
    path = Path(run_dir) / "config.json" if run_dir else None
    if path and path.exists():
        saved = json.loads(path.read_text(encoding="utf-8")).get("roles") or {}
        for role, spec in saved.items():
            if role in roles:
                roles[role] = replace(roles[role], **{k: v for k, v in spec.items()
                                                      if k in ("provider", "model", "effort",
                                                               "options")})
    for text in flags:
        role, spec = parse(text)
        for name in (ROLES if role == "all" else (role,)):
            roles[name] = spec
    return roles


def save(run_dir: str | Path, roles: dict[str, RoleConfig]) -> None:
    """Write the roles a run uses, so --resume runs on the same ones."""
    path = Path(run_dir) / "config.json"
    path.write_text(json.dumps({"roles": {
        role: {"provider": c.provider, "model": c.model, "effort": c.effort,
               "options": c.options} for role, c in roles.items()}}, indent=2) + "\n")
