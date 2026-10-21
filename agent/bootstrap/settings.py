"""
WHAT:  The design agent's own settings -- which model each of its three model
       nodes runs on, and every prompt it sends -- as something a session can
       look at and change BEFORE the conversation starts.
WHY:   The bootstrap graph is hand-written and fixed, which is right for its
       topology and wrong for its knobs. Its prompts lived only in prompts.py
       and its backends only in config resolution, so "what is about to
       interview me, and what has it been told?" had no answer short of
       reading the source -- and changing either meant editing it.
CONCEPT: Per-session overrides on top of defaults, stored as a DIFF.

--------------------------------------------------------------------------
THE FILE
--------------------------------------------------------------------------
    <session>/bootstrap.json

    {"format_version": 1,
     "nodes": {"designer": {"provider": "claude_code", "model": "opus",
                            "options": {"timeout": 900},
                            "prompts": {"instructions": "..."}}},
     "max_design_attempts": 5}

Only what DIFFERS from the defaults is written. Two reasons: an untouched
prompt keeps following prompts.py as it improves, and the file answers "what
did I change?" by being exactly that list.

It is session-scoped rather than a repo setting because the prompts are the
experiment. The repo-wide way to move a role to another model --
<repo>/.agent/config.json -- still works and is what these defaults show.

--------------------------------------------------------------------------
WHAT IS EDITABLE
--------------------------------------------------------------------------
Everything static. The per-turn prompts are built from state -- the request,
the transcript, the plan -- so they are shown as a description of what they
carry, not as a template: a template would have to re-implement the Python
that assembles them, and would drift from it the first time either changed.
"""

from __future__ import annotations

import inspect
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.bootstrap import prompts as P

FILE_NAME = "bootstrap.json"

#: The nodes that call a model, in graph order. Their names are also their
#: config roles -- see runtime.BOOTSTRAP_ACCESS.
MODEL_NODES: tuple[str, ...] = ("discussor", "researcher", "planner", "designer")

#: Every node, in the order a reader should meet them.
ALL_NODES: tuple[str, ...] = ("discussor", "researcher", "human", "planner",
                              "designer", "writer", "validator")

#: The access each model node runs with: read_only, except the researcher,
#: which runs its lookups as commands (and antigravity has no read_only).
def node_access(node: str):
    from agent.runtime import BOOTSTRAP_ACCESS, RESEARCH_ACCESS

    return {**BOOTSTRAP_ACCESS, **RESEARCH_ACCESS}[node]

#: A shared designer retry budget cannot sensibly exceed this: each attempt is
#: a full design turn, the most expensive call in the project.
MAX_ATTEMPTS_LIMIT = 20


# ---------------------------------------------------------------------------
# the static prompts, and where each one is sent
# ---------------------------------------------------------------------------

def designer_reference() -> str:
    """The bulky reference appended to the designer's first prompt."""
    return (P.RESEARCH_CONTEXT + P.VERIFIER_LOOP + P.PROMPT_REFERENCE
            + P.CONTROL_FLOW)


#: node -> field -> (label, where it is sent, default text).
#: A function, because designer_reference() renders JSON and there is no
#: reason to pay for that at import time.
def prompt_fields() -> dict[str, dict[str, tuple[str, str, str]]]:
    from agent import skills

    return {
        "discussor": {
            "instructions": (
                "Instructions",
                "Standing persona, sent as the developer/system prompt on "
                "EVERY turn.",
                P.DISCUSSOR_INSTRUCTIONS),
            "checklist": (
                "Experiment design checklist (skill)",
                "Sent in the first turn's prompt: what the discussor works "
                "through, asking only what is still open. From "
                "agent/skills/experiment_design.md.",
                skills.experiment_design()),
        },
        "researcher": {
            "instructions": (
                "Instructions",
                "Standing persona, sent as the developer/system prompt on "
                "EVERY research turn.",
                P.RESEARCHER_INSTRUCTIONS),
            "skills": (
                "Research skills",
                "Sent in every research prompt: the paper and Hugging Face "
                "lookups it can run, and how to research. {tools} is replaced "
                "with the command. From agent/skills/research.md.",
                skills.research_template()),
        },
        "planner": {
            "instructions": (
                "Instructions",
                "Standing persona, sent as the developer/system prompt on "
                "EVERY turn, including /revise turns.",
                P.PLANNER_INSTRUCTIONS),
        },
        "designer": {
            "instructions": (
                "Instructions",
                "Standing rules, sent as the developer/system prompt on EVERY "
                "turn, including repair turns. Keep it short: on codex, "
                "instructions past ~6,000 characters have been measured to "
                "hang the turn.",
                P.DESIGNER_INSTRUCTIONS),
            "reference": (
                "Reference (appended to the first design prompt)",
                "The format reference, sent once in the first design turn's "
                "prompt, after the conversation and the approved plan and "
                "before the starting design. Repair turns rely on "
                "the provider conversation remembering it.",
                designer_reference()),
            "repair_prefix": (
                "Repair prefix",
                "Opens a repair turn, followed by the validator's list of "
                "problems with the previous design.",
                P.REPAIR_PREFIX),
        },
    }


#: What the per-turn prompt of each model node carries. Shown, not edited --
#: see the module docstring.
TURN_PROMPTS = {
    "discussor": (
        "First turn: the task you typed, and an instruction to ask every "
        "question it still needs at once, each with a suggested answer. Later "
        "turns: your reply (unanswered questions keep their suggestion), and an "
        "instruction to ask only the follow-ups it opened, or say it is ready."),
    "researcher": (
        "Your request, what the discussor has settled so far, and its research "
        "questions (up to three), then the Research skills above. Runs only "
        "when research is on for the session and the discussor asks."),
    "planner": (
        "Your request, the whole conversation, and the requirements the "
        "discussor settled on, then: inspect the repository and plan it as the "
        "four stages. A /revise turn sends only what you asked to change."),
    "designer": (
        "The conversation (newest 12,000 characters), the discussor's "
        "requirements, any guidance given with /plan, and THE APPROVED PLAN "
        "with every step id, then the Reference above, then the STARTING "
        "AGENT code built from the plan, as JSON, to edit. A repair turn sends "
        "the Repair prefix, the validator's problems and the agent as it "
        "stands instead."),
}

SUMMARIES = {
    "discussor": "Interviews you about the experiment: reads the repository, "
                 "then asks everything still open at once, each with a "
                 "suggested answer -- after asking the researcher, when "
                 "research is on. Only you can end it, with /plan.",
    "researcher": "Looks up what the discussor's suggestions depend on -- "
                  "papers (arXiv, Hugging Face), datasets, models, citations, "
                  "and the web -- and reports findings with their sources. "
                  "On only when the session's research box is ticked.",
    "human": "Where the graph stops and waits for you. Plain text goes back "
             "to the discussor; the slash commands move the design along.",
    "planner": "Turns the discussion into a numbered four-stage plan and "
               "writes plan.json. Stops for your /approve -- you can edit the "
               "plan first.",
    "designer": "Designs the agent by editing a working one that code built "
                "from the plan: returns the whole graph.json, plus entries for "
                "only the nodes it adds or changes, and the brief.",
    "writer": "No model. Writes the design to agent.tmp/ on disk.",
    "validator": "No model. Loads the folder from disk and checks it. On "
                 "problems it sends them back to the designer, up to the "
                 "attempt limit, then falls back to the last agent that "
                 "passed; either way it asks you to review the agent.",
}


# ---------------------------------------------------------------------------
# the file
# ---------------------------------------------------------------------------

class _NodeFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str | None = None
    model: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    prompts: dict[str, str] = Field(default_factory=dict)


class _SettingsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format_version: int = 1
    nodes: dict[str, _NodeFile] = Field(default_factory=dict)
    max_design_attempts: int | None = Field(default=None, ge=1, le=MAX_ATTEMPTS_LIMIT)


class SettingsError(ValueError):
    """An override that cannot be applied. `where` names the field."""

    def __init__(self, where: str, message: str):
        super().__init__(message)
        self.where = where
        self.message = message


def path_for(session_dir: Path | str) -> Path:
    return Path(session_dir) / FILE_NAME


def load(session_dir: Path | str) -> _SettingsFile:
    """The overrides for this session; empty when there are none.

    A file that does not parse is an ERROR, not an empty set of overrides:
    silently running the defaults after someone edited the prompts is the
    "I changed it and nothing happened" afternoon this project keeps refusing.
    """
    path = path_for(session_dir)
    if not path.exists():
        return _SettingsFile()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        parsed = _SettingsFile.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise SettingsError(str(path), f"{path.name} is not valid: {exc}") from None
    _check_names(parsed)
    return parsed


def _check_names(parsed: _SettingsFile) -> None:
    fields = prompt_fields()
    for node, entry in parsed.nodes.items():
        if node not in MODEL_NODES:
            raise SettingsError(f"nodes.{node}",
                                f"{node!r} is not a model node of the design "
                                f"agent. Those are: {', '.join(MODEL_NODES)}.")
        for name in entry.prompts:
            if name not in fields[node]:
                raise SettingsError(f"nodes.{node}.prompts.{name}",
                                    f"{node} has no prompt called {name!r}. It "
                                    f"has: {', '.join(fields[node])}.")


def _write(session_dir: Path | str, parsed: _SettingsFile) -> Path:
    """Atomically: a half-written settings file would refuse to load."""
    path = path_for(session_dir)
    body = parsed.model_dump(mode="json", exclude_defaults=False)
    body["nodes"] = {
        name: _dump_node(entry) for name, entry in parsed.nodes.items()
        if _dump_node(entry)
    }
    if body.get("max_design_attempts") is None:
        body.pop("max_design_attempts", None)
    if not body["nodes"] and "max_design_attempts" not in body:
        path.unlink(missing_ok=True)
        return path

    handle = tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=".bootstrap.", suffix=".tmp",
        delete=False, encoding="utf-8")
    try:
        json.dump(body, handle, indent=2)
        handle.write("\n")
        handle.close()
        os.replace(handle.name, path)
    except BaseException:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise
    return path


def _dump_node(entry: _NodeFile) -> dict:
    out: dict[str, Any] = {}
    # model_fields_set, so an explicit "provider default" (null) survives.
    for key in ("provider", "model"):
        if key in entry.model_fields_set:
            out[key] = getattr(entry, key)
    if entry.options:
        out["options"] = entry.options
    if entry.prompts:
        out["prompts"] = entry.prompts
    return out


def reset(session_dir: Path | str) -> None:
    path_for(session_dir).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# applying them
# ---------------------------------------------------------------------------

def effective_prompts(parsed: _SettingsFile) -> dict[str, dict[str, str]]:
    """node -> field -> the text that will actually be sent."""
    out = {}
    for node, fields in prompt_fields().items():
        chosen = parsed.nodes.get(node, _NodeFile()).prompts
        out[node] = {name: chosen.get(name, default)
                     for name, (_, _, default) in fields.items()}
    return out


def effective_config(cfg, parsed: _SettingsFile):
    """`cfg` with this session's backend overrides for the three roles.

    Used ONLY to build the design phase's backends. The work phase keeps the
    unmodified config, so a generated node that happens to reuse the name
    "planner" is not silently moved along with it.
    """
    from agent.config import BackendConfig, backend_for

    roles = dict(cfg.roles)
    for node, entry in parsed.nodes.items():
        if not ({"provider", "model"} & entry.model_fields_set or entry.options):
            continue
        base = backend_for(cfg, node)
        provider = entry.provider or base.provider
        if "model" in entry.model_fields_set:
            # "" rather than None for "the provider's own default": None
            # would fall through to the DEFAULT role's model in backend_for,
            # which belongs to another provider when this one was changed.
            model = entry.model or ""
        else:
            model = base.model if provider == base.provider else ""
        roles[node] = BackendConfig(provider=provider, model=model,
                                    options={**base.options, **entry.options})

    attempts = parsed.max_design_attempts or cfg.max_design_attempts
    return replace(cfg, roles=roles, max_design_attempts=attempts)


# ---------------------------------------------------------------------------
# describing a backend's options
# ---------------------------------------------------------------------------

#: Constructor parameters that are not options a person sets.
_NOT_OPTIONS = {"self", "client", "model", "options"}

_OPTION_HELP = {
    "timeout": "Seconds of SILENCE before a turn is abandoned (not total time).",
    "max_seconds": "Absolute cap on one turn, in seconds.",
    "max_output_tokens": "Cap on how much answer one turn may stream.",
    "busy_attempts": "Retries when the provider says it is overloaded.",
    "credit_wait_hours": "How long, in all, to wait out an empty account or a usage limit (24).",
    "credit_wait_seconds": "The first wait for credits; later ones double, up to 30 minutes.",
    "credit_wait_attempts": "Optional cap on the number of credit checks as well.",
    "executable": "The CLI binary to run.",
    "gpu": "Codex only: run write turns WITHOUT Codex's sandbox, so commands can "
           "use the GPUs (the sandbox hides them). Staying in the session's "
           "workspace then rests on the instructions.",
    "max_turns": "Cap on the CLI's own tool-use turns (empty = no cap).",
    "max_budget_usd": "Stop a turn past this spend (empty = no cap).",
    "effort": "Reasoning effort: low, medium or high (empty = CLI default).",
}


def option_fields(provider: str) -> list[dict]:
    """The options a provider's backend accepts, read from its constructor.

    Read, not listed: a hand-kept list is how a UI offers an option the
    backend no longer takes. Walks the MRO because the CLI backends pass
    **options up to CliBackend, which is where timeout lives.
    """
    from agent.backends import _load

    try:
        cls = _load(provider)
    except Exception:  # noqa: BLE001 - an unknown or unimportable provider
        return []

    seen: dict[str, dict] = {}
    for klass in cls.__mro__:
        init = klass.__dict__.get("__init__")
        if init is None:
            continue
        for name, param in inspect.signature(init).parameters.items():
            if name in _NOT_OPTIONS or name in seen:
                continue
            if param.kind in (param.VAR_KEYWORD, param.VAR_POSITIONAL):
                continue
            default = None if param.default is param.empty else param.default
            seen[name] = {"name": name, "type": _type_of(param, default),
                          "default": default, "help": _OPTION_HELP.get(name, "")}
    return list(seen.values())


def _type_of(param, default) -> str:
    if isinstance(default, bool):
        return "boolean"
    if isinstance(default, int):
        return "integer"
    if isinstance(default, float):
        return "number"
    annotation = str(param.annotation)
    if "float" in annotation:
        return "number"
    if "int" in annotation:
        return "integer"
    return "string"


def _coerce(provider: str, options: dict[str, Any]) -> dict[str, Any]:
    fields = {f["name"]: f for f in option_fields(provider)}
    out: dict[str, Any] = {}
    for key, value in (options or {}).items():
        if key not in fields:
            known = ", ".join(sorted(fields)) or "(none)"
            raise SettingsError(f"options.{key}",
                                f"The {provider} backend has no option {key!r}. "
                                f"It takes: {known}.")
        if value is None or value == "":
            continue                    # "use the default"
        kind = fields[key]["type"]
        try:
            if kind == "integer":
                value = int(value)
            elif kind == "number":
                value = float(value)
            elif kind == "boolean":
                value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
            else:
                value = str(value)
        except (TypeError, ValueError):
            raise SettingsError(f"options.{key}",
                                f"{key} must be a {kind}, got {value!r}.") from None
        if kind in ("integer", "number") and value <= 0:
            raise SettingsError(f"options.{key}", f"{key} must be positive.")
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# saving from the editor
# ---------------------------------------------------------------------------

def save(session_dir: Path | str, cfg, submitted: dict) -> tuple[_SettingsFile, list[str]]:
    """Store what the editor sent, as a diff against the defaults.

    `submitted` has the editor's shape -- full values, not a diff:
        {"nodes": {"designer": {"provider", "model", "options", "prompts"}},
         "max_design_attempts": 3}
    A node left out keeps its current overrides; a value equal to its default
    is dropped, which is how "Reset" works without a second endpoint.

    Returns the stored overrides and any warnings. Raises SettingsError for
    anything that would fail at start -- checked here, where the person who
    made the change is still looking at it.
    """
    from agent.backends import PROVIDERS, _check_access, _load
    from agent.backends.base import Access, BackendUnavailable
    from agent.config import BackendConfig, backend_for

    if not isinstance(submitted, dict):
        raise SettingsError("body", "Expected a JSON object.")
    unknown = set(submitted) - {"nodes", "max_design_attempts"}
    if unknown:
        raise SettingsError(sorted(unknown)[0], f"Unknown field {sorted(unknown)[0]!r}.")

    current = load(session_dir)
    nodes = dict(current.nodes)
    fields = prompt_fields()

    for node, entry in (submitted.get("nodes") or {}).items():
        if node not in MODEL_NODES:
            raise SettingsError(f"nodes.{node}",
                                f"{node!r} has no settings. Only "
                                f"{', '.join(MODEL_NODES)} call a model.")
        if not isinstance(entry, dict):
            raise SettingsError(f"nodes.{node}", "Expected an object.")
        extra = set(entry) - {"provider", "model", "options", "prompts"}
        if extra:
            raise SettingsError(f"nodes.{node}.{sorted(extra)[0]}",
                                f"Unknown field {sorted(extra)[0]!r}.")

        base = backend_for(cfg, node)
        stored: dict[str, Any] = {}

        provider = str(entry.get("provider") or base.provider).strip()
        if provider not in PROVIDERS:
            raise SettingsError(f"nodes.{node}.provider",
                                f"Unknown provider {provider!r}. Known: "
                                f"{', '.join(sorted(PROVIDERS))}.")
        model = entry.get("model", base.model if provider == base.provider else "")
        model = (str(model).strip() if model is not None else "")
        if provider != base.provider:
            stored["provider"] = provider
            stored["model"] = model or None
        elif model != (base.model or ""):
            stored["model"] = model or None

        options = _coerce(provider, entry.get("options") or {})
        defaults = {f["name"]: f["default"] for f in option_fields(provider)}
        defaults.update(base.options if provider == base.provider else {})
        diff = {k: v for k, v in options.items() if defaults.get(k) != v}
        if diff:
            stored["options"] = diff

        # Checked now, not at Start: agy cannot express read_only, and the
        # three design roles are read_only. build_backends would refuse the
        # same thing -- after you had pressed Start.
        try:
            _check_access(node, _load(provider),
                          BackendConfig(provider=provider, model=model),
                          node_access(node))
        except BackendUnavailable as exc:
            raise SettingsError(f"nodes.{node}.provider", str(exc)) from None

        prompts_in = entry.get("prompts")
        if prompts_in is None:
            prompts_out = dict(current.nodes.get(node, _NodeFile()).prompts)
        else:
            if not isinstance(prompts_in, dict):
                raise SettingsError(f"nodes.{node}.prompts", "Expected an object.")
            prompts_out = {}
            for name, text in prompts_in.items():
                if name not in fields[node]:
                    raise SettingsError(f"nodes.{node}.prompts.{name}",
                                        f"{node} has no prompt called {name!r}.")
                text = "" if text is None else str(text)
                if not text.strip():
                    raise SettingsError(
                        f"nodes.{node}.prompts.{name}",
                        f"{node}'s {name} is empty. Use Reset to go back to "
                        f"the default rather than sending nothing.")
                if text.strip() != fields[node][name][2].strip():
                    prompts_out[name] = text
        if prompts_out:
            stored["prompts"] = prompts_out

        if stored:
            nodes[node] = _NodeFile.model_validate(stored)
        else:
            nodes.pop(node, None)

    attempts = submitted.get("max_design_attempts", current.max_design_attempts)
    if attempts in ("", None) or attempts == cfg.max_design_attempts:
        attempts = None
    else:
        try:
            attempts = int(attempts)
        except (TypeError, ValueError):
            raise SettingsError("max_design_attempts",
                                "max_design_attempts must be a whole number.") from None
        if not 1 <= attempts <= MAX_ATTEMPTS_LIMIT:
            raise SettingsError("max_design_attempts",
                                f"max_design_attempts must be 1-{MAX_ATTEMPTS_LIMIT}.")

    parsed = _SettingsFile(nodes=nodes, max_design_attempts=attempts)
    _write(session_dir, parsed)
    return parsed, warnings_for(cfg, parsed)


def warnings_for(cfg, parsed: _SettingsFile) -> list[str]:
    """What will work but probably should not be done."""
    from agent.backends._cli import MAX_ARG_BYTES
    from agent.backends.codex import SAFE_INSTRUCTIONS_CHARS
    from agent.config import backend_for

    effective = effective_config(cfg, parsed)
    texts = effective_prompts(parsed)
    out = []
    for node in MODEL_NODES:
        provider = backend_for(effective, node).provider
        instructions = texts[node]["instructions"]
        if provider == "codex" and len(instructions) > SAFE_INSTRUCTIONS_CHARS:
            out.append(
                f"{node}: instructions are {len(instructions):,} characters. On "
                f"codex, past about {SAFE_INSTRUCTIONS_CHARS:,} a turn has been "
                f"measured to hang instead of replying. Move bulk into the "
                f"Reference instead.")
        if provider == "antigravity":
            # agy takes persona and prompt as ONE argv entry.
            biggest = len(instructions.encode()) + sum(
                len(t.encode()) for k, t in texts[node].items() if k != "instructions")
            if biggest > MAX_ARG_BYTES * 0.8:
                out.append(
                    f"{node}: antigravity passes the prompt on the command line, "
                    f"which Linux caps at {MAX_ARG_BYTES // 1024} KB per "
                    f"argument; these prompts are already "
                    f"{biggest // 1024} KB before the conversation is added.")
    return out


# ---------------------------------------------------------------------------
# the whole picture, for the editor and for --explain
# ---------------------------------------------------------------------------

def describe(cfg, session_dir: Path | str) -> dict:
    """Every node, its settings and its prompts, with defaults beside them."""
    from agent.backends import PROVIDERS, _load
    from agent.bootstrap.commands import BOOTSTRAP_REGISTRY
    from agent.config import DEFAULT_MODEL, DEFAULT_ROLE_BACKENDS, backend_for

    parsed = load(session_dir)
    effective = effective_config(cfg, parsed)
    texts = effective_prompts(parsed)
    fields = prompt_fields()

    nodes = {}
    for node in ALL_NODES:
        info: dict[str, Any] = {"name": node, "summary": SUMMARIES[node]}
        if node in MODEL_NODES:
            spec = backend_for(effective, node)
            default = backend_for(cfg, node)
            chosen = parsed.nodes.get(node, _NodeFile())
            option_list = option_fields(spec.provider)
            for field in option_list:
                field["value"] = spec.options.get(field["name"], field["default"])
                field["modified"] = field["name"] in chosen.options
            try:
                max_access = _load(spec.provider).max_access.value
            except Exception:  # noqa: BLE001
                max_access = ""
            info.update({
                "kind": "agent",
                "access": node_access(node).value,
                "backend": {"provider": spec.provider, "model": spec.model or "",
                            "max_access": max_access,
                            "modified": bool({"provider", "model"}
                                             & chosen.model_fields_set)},
                "default_backend": {"provider": default.provider,
                                    "model": default.model or ""},
                "options": option_list,
                "prompts": [{
                    "name": name, "label": label, "sent": where,
                    "value": texts[node][name], "default": default_text,
                    "modified": name in chosen.prompts,
                    "chars": len(texts[node][name]),
                } for name, (label, where, default_text) in fields[node].items()],
                "turn_prompt": TURN_PROMPTS[node],
            })
        elif node == "human":
            info.update({"kind": "human", "commands": [
                {"name": c.name, "summary": c.summary, "argument": c.argument,
                 "contexts": list(c.contexts or ())}
                for c in BOOTSTRAP_REGISTRY if c.scope == "graph"]})
        else:
            info["kind"] = "tool"
        nodes[node] = info

    # A model to suggest when somebody switches provider, so they are never
    # left sending one provider's model id to another.
    suggested = {"codex": DEFAULT_MODEL}
    for provider, model in DEFAULT_ROLE_BACKENDS.values():
        suggested.setdefault(provider, model)
    for provider, model in _FALLBACK_MODELS.items():
        suggested.setdefault(provider, model)

    return {
        "path": str(path_for(session_dir)),
        "exists": path_for(session_dir).exists(),
        "nodes": nodes,
        "order": list(ALL_NODES),
        "max_design_attempts": {"value": effective.max_design_attempts,
                                "default": cfg.max_design_attempts,
                                "limit": MAX_ATTEMPTS_LIMIT},
        "providers": sorted(p for p in PROVIDERS if p != "api"),
        "suggested_models": suggested,
        "view": graph_view(effective),
        "warnings": warnings_for(cfg, parsed),
    }


#: For a provider no default role names -- so switching to it in the editor
#: still pre-fills a model that provider accepts.
_FALLBACK_MODELS = {"claude_code": "opus", "antigravity": "gemini-4-argon-medium"}


def render_text(cfg, session_dir: Path | str) -> str:
    """The design agent for a terminal: `run --explain` before a design exists."""
    view = describe(cfg, session_dir)
    lines = ["DESIGN AGENT (the graph that interviews you and designs yours)",
             "-" * 78]
    for name in view["order"]:
        node = view["nodes"][name]
        if node["kind"] != "agent":
            lines.append(f"  {name:10} {node['kind']:6} {node['summary'][:58]}")
            continue
        backend = node["backend"]
        mark = "  (changed)" if backend["modified"] else ""
        lines.append(f"  {name:10} model  {backend['provider']} / "
                     f"{backend['model'] or '(provider default)'}{mark}")
        for prompt in node["prompts"]:
            flag = "changed" if prompt["modified"] else "default"
            lines.append(f"  {'':10}        {prompt['name']:14} "
                         f"{prompt['chars']:>7,} chars  {flag}")
    attempts = view["max_design_attempts"]
    lines.append(f"  max design attempts {attempts['value']}")
    for warning in view["warnings"]:
        lines.append(f"  ! {warning}")
    lines.append("")
    lines.append(f"  settings file  {view['path']}"
                 + ("" if view["exists"] else "  (not written; all defaults)"))
    lines.append("  Edit it, or change everything in the web UI's Design agent tab.")
    return "\n".join(lines)


def graph_view(cfg) -> dict:
    """The fixed graph, in the shape webui/static/js/graph.js draws.

    Hand-written to match graph.py, which is itself hand-written and fixed --
    a test compiles the real graph and checks every edge here exists there.
    """
    from agent.config import backend_for

    def agent(name):
        spec = backend_for(cfg, name)
        return {"id": name, "kind": "agent", "entry": name == "discussor",
                "backend": spec.provider, "model": spec.model or "",
                "access": node_access(name).value, "steps": [], "unknown_steps": [],
                "commands": []}

    def tool(name):
        return {"id": name, "kind": "tool", "entry": False, "backend": "no model",
                "steps": [], "unknown_steps": [], "commands": []}

    nodes = [agent("discussor"), agent("researcher"),
             {"id": "human", "kind": "human", "entry": False, "steps": [],
              "unknown_steps": [], "commands": ["plan", "approve", "revise",
                                                "retry", "discuss", "exit"]},
             agent("planner"), agent("designer"), tool("writer"), tool("validator"),
             {"id": "__end__", "kind": "end", "entry": False, "steps": [],
              "unknown_steps": [], "commands": []}]

    def edge(a, b, kind="edge", label="", asks=False):
        return {"from": a, "to": b, "kind": kind, "label": label, "asks": asks}

    edges = [
        edge("discussor", "researcher", "case", "research questions"),
        edge("researcher", "discussor"),
        edge("discussor", "human", "case", "questions", asks=True),
        edge("human", "discussor", "command", "reply / /discuss"),
        edge("human", "planner", "command", "/plan /revise"),
        edge("planner", "human", asks=True),
        edge("human", "designer", "command", "/approve /retry"),
        edge("designer", "writer"),
        edge("writer", "validator"),
        edge("validator", "designer", "case", "problems"),
        edge("validator", "human", "case", "review / gave up", asks=True),
        edge("human", "__end__", "command", "/approve agent · /exit"),
    ]
    return {"name": "design agent", "description": "", "entry": "discussor",
            "nodes": nodes, "edges": edges}
