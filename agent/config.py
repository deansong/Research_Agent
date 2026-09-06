"""
WHAT:  Works out which backend each role should use, from defaults + files +
       environment + command line.
WHY:   Requirement 3: every node must be independently configurable.  This
       used to be three module-level constants and one env var applied to
       everything.
CONCEPT: Not LangGraph -- ordinary layered configuration.

--------------------------------------------------------------------------
RESOLUTION ORDER (later wins)
--------------------------------------------------------------------------
  1. DEFAULTS below                              (everything -> codex)
  2. ~/.codex-langgraph-agent/config.json        your global preferences
  3. <repo>/.agent/config.json                   per-repository settings
  4. --config <path>                             REPLACES 2 and 3
  5. environment variables                       AGENT_BACKEND, AGENT_MODEL,
                                                 AGENT_BACKEND_<ROLE>, ...
  6. command line                                --backend, --model,
                                                 --backend-role role=prov:model

Layer 4 replaces rather than stacks on 2 and 3 on purpose: "which files am I
actually reading?" should have a one-sentence answer.  Run with --explain to
print the result of all this without spending a token.
"""

from __future__ import annotations

import difflib
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, ValidationError

from agent import roles

DEFAULT_SESSION = "main"

# LangGraph raises GraphRecursionError after this many super-steps in one
# invoke(). Ours is high because a long task legitimately loops
# orchestrator -> executor -> orchestrator many times.
DEFAULT_RECURSION_LIMIT = 1000

GLOBAL_CONFIG = Path.home() / ".codex-langgraph-agent" / "config.json"
REPO_CONFIG_RELATIVE = Path(".agent") / "config.json"


@dataclass(frozen=True)
class BackendConfig:
    """Which provider serves one role, and how."""

    provider: str = "codex"
    model: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    """Provider-specific extras, passed straight to the backend's __init__.
    e.g. {"permission_mode": "acceptEdits"} for claude_code."""


@dataclass(frozen=True)
class AgentConfig:
    default: BackendConfig
    roles: dict[str, BackendConfig]
    session: str = DEFAULT_SESSION
    recursion_limit: int = DEFAULT_RECURSION_LIMIT

    # ---- phase 4: designing and running generated agents -------------------
    max_design_attempts: int = 3
    """How many times the designer may be asked to fix a bad folder before
    giving up and asking the human."""

    work_recursion_limit: int = 200
    """Step budget for a generated agent. Lower than the bootstrap graph's:
    a generated graph that loops forever should fail fast and visibly."""

    default_agent: str = "default"
    """Folder used by /use and by --pre-build-agent with no argument."""

    sources: tuple[str, ...] = ()
    """Human-readable list of where the settings came from, for --explain."""


def backend_for(cfg: AgentConfig, role: str) -> BackendConfig:
    """The effective config for one role: its own settings over the default.

    Merging per FIELD (not per role) is what lets you write
        {"default": {"model": "gpt-5.4"}, "roles": {"executor": {"provider": "codex"}}}
    and have the executor still pick up the default model.
    """
    override = cfg.roles.get(role)
    if override is None:
        return cfg.default

    return BackendConfig(
        provider=override.provider or cfg.default.provider,
        model=override.model if override.model is not None else cfg.default.model,
        options={**cfg.default.options, **override.options},
    )


# ---------------------------------------------------------------------------
# File parsing.  Pydantic (not plain json.load) so a typo like "provdier" is
# a clear error instead of a silently ignored key -- the same strictness
# lesson agent/schemas.py teaches for model output.
# ---------------------------------------------------------------------------

class _BackendFile(BaseModel):
    # extra="forbid" turns a typo into an error instead of a silently ignored
    # key. "_comment" is allowed everywhere because JSON has no comments and
    # a config file you cannot annotate is a config file nobody understands.
    model_config = ConfigDict(extra="forbid")
    provider: str | None = None
    model: str | None = None
    options: dict[str, Any] = {}


class _ConfigFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default: _BackendFile | None = None
    roles: dict[str, _BackendFile] = {}
    session: str | None = None
    recursion_limit: int | None = None
    max_design_attempts: int | None = None
    work_recursion_limit: int | None = None
    default_agent: str | None = None


def _strip_comments(value):
    """Drop any key starting with "_" , at any depth.

    JSON has no comment syntax, so the convention here is that "_comment" (or
    "_anything") is documentation. Stripping it before validation lets the
    models keep extra="forbid" -- so a real typo is still an error -- while
    the shipped agent.example.json stays readable.
    """
    if isinstance(value, dict):
        return {k: _strip_comments(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [_strip_comments(v) for v in value]
    return value


def _read_file(path: Path) -> _ConfigFile | None:
    if not path.exists():
        return None
    try:
        return _ConfigFile.model_validate(_strip_comments(json.loads(path.read_text())))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Config error in {path}: not valid JSON ({exc}).")
    except ValidationError as exc:
        raise SystemExit(f"Config error in {path}:\n{exc}")


# ---------------------------------------------------------------------------
# The layers
# ---------------------------------------------------------------------------

def load_config(
    *,
    repo_path: Path,
    config_file: Path | None = None,
    env: Mapping[str, str] | None = None,
    cli: Any = None,
) -> AgentConfig:
    env = os.environ if env is None else env

    # ---- layer 1: built-in defaults ------------------------------------
    default = BackendConfig(provider="codex", model=None, options={})
    role_configs: dict[str, BackendConfig] = {}
    session = DEFAULT_SESSION
    recursion_limit = DEFAULT_RECURSION_LIMIT
    sources: list[str] = ["built-in defaults"]
    extras: dict[str, Any] = {}

    # ---- layers 2-4: files ---------------------------------------------
    if config_file is not None:
        paths = [config_file]
        if not config_file.exists():
            raise SystemExit(f"Config file not found: {config_file}")
    else:
        paths = [GLOBAL_CONFIG, repo_path / REPO_CONFIG_RELATIVE]

    for path in paths:
        parsed = _read_file(path)
        if parsed is None:
            continue
        sources.append(str(path))

        if parsed.default is not None:
            default = _apply_file(default, parsed.default)
        for role_name, entry in parsed.roles.items():
            # NOT an error any more. A generated agent folder can name a role
            # anything it likes ("reviewer", "summariser"), and you are allowed
            # to configure that role before the folder exists. So an unknown
            # name is a WARNING with a typo suggestion, not a hard stop.
            _warn_unknown_role(role_name, f"in {path}")
            role_configs[role_name] = _apply_file(
                role_configs.get(role_name, BackendConfig(provider="", model=None)), entry
            )
        if parsed.session:
            session = parsed.session
        if parsed.recursion_limit:
            recursion_limit = parsed.recursion_limit
        for name in ("max_design_attempts", "work_recursion_limit", "default_agent"):
            value = getattr(parsed, name)
            if value is not None:
                extras[name] = value

    # ---- layer 5: environment -------------------------------------------
    # CODEX_MODEL is the pre-refactor name; still honoured so old shell
    # profiles keep working.
    legacy_model = env.get("CODEX_MODEL")
    if legacy_model:
        default = replace(default, model=legacy_model)
        sources.append("CODEX_MODEL (deprecated -- prefer AGENT_MODEL)")

    if env.get("AGENT_BACKEND"):
        default = replace(default, provider=env["AGENT_BACKEND"])
        sources.append("AGENT_BACKEND")
    if env.get("AGENT_MODEL"):
        default = replace(default, model=env["AGENT_MODEL"])
        sources.append("AGENT_MODEL")

    for role_name in roles.ALL_ROLES:
        suffix = role_name.upper()
        provider = env.get(f"AGENT_BACKEND_{suffix}")
        model = env.get(f"AGENT_MODEL_{suffix}")
        if provider or model:
            existing = role_configs.get(role_name, BackendConfig(provider="", model=None))
            role_configs[role_name] = replace(
                existing,
                provider=provider or existing.provider,
                model=model if model is not None else existing.model,
            )
            sources.append(f"AGENT_*_{suffix}")

    # ---- layer 6: command line -------------------------------------------
    if cli is not None:
        if getattr(cli, "backend", None):
            default = replace(default, provider=cli.backend)
            sources.append("--backend")
        if getattr(cli, "model", None):
            default = replace(default, model=cli.model)
            sources.append("--model")
        for item in getattr(cli, "backend_role", None) or []:
            role_name, spec = _parse_role_override(item)
            existing = role_configs.get(role_name, BackendConfig(provider="", model=None))
            role_configs[role_name] = replace(
                existing,
                provider=spec.provider or existing.provider,
                model=spec.model if spec.model is not None else existing.model,
            )
            sources.append(f"--backend-role {item}")
        if getattr(cli, "session", None):
            session = cli.session

    return AgentConfig(
        default=default,
        roles=role_configs,
        session=session,
        recursion_limit=recursion_limit,
        sources=tuple(sources),
        **extras,
    )


def _apply_file(base: BackendConfig, entry: _BackendFile) -> BackendConfig:
    return BackendConfig(
        provider=entry.provider or base.provider,
        model=entry.model if entry.model is not None else base.model,
        options={**base.options, **entry.options},
    )


def _parse_role_override(item: str) -> tuple[str, BackendConfig]:
    """Parse "executor=claude_code:opus" into ("executor", BackendConfig(...))."""
    if "=" not in item:
        raise SystemExit(
            f"--backend-role expects role=provider[:model], got '{item}'.\n"
            f"Example: --backend-role executor=codex:gpt-5.4"
        )
    role_name, spec = item.split("=", 1)
    role_name = role_name.strip()

    _warn_unknown_role(role_name, "in --backend-role")

    provider, _, model = spec.partition(":")
    return role_name, BackendConfig(provider=provider.strip(), model=model.strip() or None)


def _warn_unknown_role(role_name: str, where: str) -> None:
    """Warn (do not fail) about a role name we do not recognise.

    Role names come from two places now: the four built-in ones, and whatever
    a generated agent folder invented. We cannot know the second set until a
    folder is loaded, so we can only offer a typo suggestion.
    """
    if role_name in roles.ALL_ROLES:
        return

    suggestion = difflib.get_close_matches(role_name, roles.ALL_ROLES, n=1, cutoff=0.7)
    hint = f" Did you mean '{suggestion[0]}'?" if suggestion else ""
    print(
        f"Note: role '{role_name}' {where} is not one of the built-in roles "
        f"({', '.join(roles.ALL_ROLES)}).{hint}\n"
        f"      That is fine if a generated agent folder defines it."
    )


def describe(cfg: AgentConfig) -> str:
    """One line per role, plus where the settings came from.  Used by /config."""
    lines = ["BACKENDS", "-" * 78]
    for role_name in roles.ALL_ROLES:
        spec = backend_for(cfg, role_name)
        needed = roles.REQUIRED_ACCESS[role_name].value
        model = spec.model or "(provider default)"
        lines.append(f"  {role_name:14} {spec.provider:14} {model:24} needs {needed}")

    lines.append("")
    lines.append(f"  session          {cfg.session}")
    lines.append(f"  recursion limit  {cfg.recursion_limit}")
    lines.append("")
    lines.append("  settings from: " + ", ".join(cfg.sources))
    return "\n".join(lines)
