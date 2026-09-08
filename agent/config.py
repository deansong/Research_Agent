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

#: The model every role gets unless something names another one.
#:
#: This used to be None, which meant "send no model and let the Codex CLI use
#: whatever ~/.codex/config.toml says". Pinning it is better for one reason:
#: the design phase writes a document of tens of thousands of tokens in one
#: turn, so which model runs it decides whether the folder validates -- and
#: that should be a decision in the repository, visible in --explain, rather
#: than whatever a machine's CLI config happens to say.
#:
#: WHICH model, from the tier table inside the codex binary:
#:
#:     gpt-5.6-sol     "Sol is the flagship-equivalent tier"
#:     gpt-5.6-terra   "Terra is the mini-like tier"
#:     gpt-5.6-luna    "Luna is the nano-like tier"
#:
#: Sol, because it is the strongest of the three and because it is what this
#: project has actually been running: with model=None we inherited
#: ~/.codex/config.toml, which says gpt-5.6-sol, and the rollout logs confirm
#: every turn used it. So this pins current behaviour rather than changing it.
#:
#: The cost of naming it here is that it now WINS over ~/.codex/config.toml:
#: there is no value meaning "inherit", because an explicit null in a config
#: file is indistinguishable from an absent key once Pydantic has parsed it.
#: To use another model, name it -- --model, --backend-role, or
#: `default.model` in <repo>/.agent/config.json.
DEFAULT_MODEL = "gpt-5.6-sol"

#: Roles that get something other than DEFAULT_MODEL out of the box.
#:
#: The split the research skeleton already sets up: `coder` writes code and
#: reports and takes the default, while `runner` and `checker` -- the roles
#: that execute experiments and judge the results -- go to the mini tier.
#: Running an experiment is mostly obedience: take this command, run it, put
#: the numbers there. Designing one is not.
#:
#: `checker` is the one to watch. It decides whether a run measured the right
#: thing, which is judgement rather than obedience, and a run that finished
#: cleanly while measuring the wrong thing is the failure it exists to catch.
#: Move it back with {"roles": {"checker": {"model": "gpt-5.6-sol"}}} if it
#: starts waving work through.
#:
#: Seeded with provider="" so that --backend fake still reaches these roles:
#: backend_for() falls back to the default provider for an empty one.
DEFAULT_ROLE_MODELS = {
    "runner": "gpt-5.6-terra",
    "checker": "gpt-5.6-terra",
}

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
    """None means "send no model and let the provider choose".

    Only reachable in code now, not through a config file: resolve() starts
    from DEFAULT_MODEL, and a file cannot express "go back to None"."""
    options: dict[str, Any] = field(default_factory=dict)
    """Provider-specific extras, passed straight to the backend's __init__.
    e.g. {"permission_mode": "acceptEdits"} for claude_code."""


@dataclass(frozen=True)
class AgentConfig:
    default: BackendConfig
    roles: dict[str, BackendConfig]
    session: str | None = None
    """None means "derive a name from the task" -- see storage.session_name_for.
    A fixed default would make every task share one session, and a session owns
    exactly one task."""

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
        {"default": {"model": "gpt-5.6-sol"}, "roles": {"executor": {"provider": "codex"}}}
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
    default = BackendConfig(provider="codex", model=DEFAULT_MODEL, options={})
    role_configs: dict[str, BackendConfig] = {
        role: BackendConfig(provider="", model=model)
        for role, model in DEFAULT_ROLE_MODELS.items()
    }
    session: str | None = None
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

    # Scan for ANY AGENT_BACKEND_<ROLE> / AGENT_MODEL_<ROLE>, rather than only
    # the roles we happen to name in roles.py. A generated agent invents its
    # own role names, so whitelisting meant AGENT_BACKEND_REVIEWER was silently
    # ignored while the same override worked fine from a config file or the
    # command line.
    for key, value in sorted(env.items()):
        # `attr`, not `field`: dataclasses.field is imported at the top of this
        # module, and shadowing it inside a loop is the kind of thing that is
        # harmless until somebody adds a line that needs the real one.
        for prefix, attr in (("AGENT_BACKEND_", "provider"), ("AGENT_MODEL_", "model")):
            if not key.startswith(prefix) or not value:
                continue
            role_name = key[len(prefix):].lower()
            if not role_name:
                continue
            existing = role_configs.get(role_name, BackendConfig(provider="", model=None))
            role_configs[role_name] = replace(existing, **{attr: value})
            sources.append(key)

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

    # A GLOBAL model override means "use this everywhere", and the built-in
    # per-role models must not outrank it. `--model gpt-5.6-pro` that quietly
    # left two roles on the mini tier would be the kind of half-applied
    # setting you only discover from a bill.
    #
    # Only the seeded value is released: if any later layer named a model for
    # that role, it stays, because somebody asked for it specifically.
    if default.model != DEFAULT_MODEL:
        for role, seeded in DEFAULT_ROLE_MODELS.items():
            current = role_configs.get(role)
            if current is not None and current.model == seeded:
                role_configs[role] = replace(current, model=None)

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
            f"Example: --backend-role executor=codex:gpt-5.6-sol"
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


def describe(cfg: AgentConfig, paths=None) -> str:
    """One line per role, plus where the settings came from.  Used by /config.

    Which roles to list is not obvious any more.  Role names are open-ended --
    a generated agent invents its own -- so there is no complete list to print.
    We show the union of the roles WE name in Python (roles.ALL_ROLES) and the
    roles YOU configured, which is the set where "what will this resolve to?"
    is a question someone might actually be asking.  A role a folder invents
    and you never override resolves to `default`, shown on its own line.

    Deliberately no "needs <access>" column: access is per NODE now, and lives
    in the folder's nodes.json, which this function has not been given.
    """
    listed = list(roles.ALL_ROLES) + [r for r in sorted(cfg.roles) if r not in roles.ALL_ROLES]

    lines = ["BACKENDS", "-" * 78]
    for role_name in listed:
        spec = backend_for(cfg, role_name)
        model = spec.model or "(provider default)"
        origin = "" if role_name in cfg.roles else "  (falls back to default)"
        lines.append(f"  {role_name:14} {spec.provider:14} {model:24}{origin}")

    lines.append("")
    # Show the FOLDER once one has been resolved. `cfg.session` is only the
    # name you typed, and is None for both of the other two routes in (derived
    # from the task, or --session-dir) -- printing that alone told you least
    # exactly when you most wanted to know where the files went.
    if paths is not None:
        lines.append(f"  session folder   {paths.session}")
        if paths.input_brief.exists():
            lines.append(f"  task read from   {paths.input_brief}")
    else:
        lines.append(f"  session          {cfg.session or '(derived from the task)'}")
    lines.append(f"  recursion limit  {cfg.recursion_limit}")
    lines.append("")
    lines.append("  settings from: " + ", ".join(cfg.sources))
    return "\n".join(lines)
