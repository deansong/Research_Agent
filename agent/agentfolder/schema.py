"""
WHAT:  Pydantic models for graph.json and nodes.json.
WHY:   One declaration of what a folder may contain, used to parse it, to
       report errors against it, and (via model_json_schema) to tell the
       designer model what to produce.
CONCEPT: Not LangGraph. Same strictness lesson as agent/schemas.py:
       extra="forbid" turns a typo into a loud error instead of a silently
       ignored key.

NOTE ON CHECKPOINTS: none of these classes ever goes into graph state. An
AgentFolder is built from disk at process start and lives only in memory;
state stores the folder PATH. This matters because a Pydantic object stored in
a loosely-annotated state field saves fine and then reloads as a plain dict,
with only a line on stderr -- a silent type change. If you ever cache a parsed
folder in state, store model_dump() and re-validate on read.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Stricter than LangGraph on purpose. LangGraph rejects "__start__" and names
# containing ":", but it silently ACCEPTS "" and "A-B" (measured). Node names
# end up in state keys, prompts and error messages, so we keep them boring.
NODE_NAME = r"^[a-z][a-z0-9_]{0,31}$"
TARGET = r"^([a-z][a-z0-9_]{0,31}|__end__)$"
FIELD_NAME = r"^[a-z][a-z0-9_]{0,31}$"

END_TARGET = "__end__"

# Output field names the `capture` mechanism writes. A node may not declare
# them itself or the model's value would be overwritten without warning.
RESERVED_OUTPUT_FIELDS = frozenset({"git_status", "git_diff", "turn_changes"})


class FolderModel(BaseModel):
    """Base for everything in a folder. Forbids unexpected keys."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# graph.json -- topology only
# ---------------------------------------------------------------------------

class AskSpec(FolderModel):
    """What to ask the human when a transition targets a `human` node.

    This is the declarative form of what today's nodes do by hand when they set
    human.question / human.context / human.purpose / human.return_to.
    """

    purpose: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    """Why we are asking. Gates which slash commands are legal here, and shows
    up in /help. A generated agent may invent its own purposes."""

    resume_to: str = Field(pattern=TARGET)
    """Where a plain-text answer goes. Usually the node that asked."""

    question: str = ""
    context: str = ""


class NodeRef(FolderModel):
    """One entry in graph.json's node list. Config lives in nodes.json."""

    name: str = Field(pattern=NODE_NAME)
    kind: Literal["agent", "human", "orchestrator"]
    """"orchestrator" is the hub of a hub agent: it decides, turn by turn,
    which node runs next -- see OrchestratorNodeConfig."""


class EdgeSpec(FolderModel):
    """An unconditional "always go here next" edge."""

    from_: str = Field(alias="from", pattern=NODE_NAME)
    to: str = Field(pattern=TARGET)
    ask: AskSpec | None = None


class CaseSpec(FolderModel):
    when: str
    """The value of the routing field that selects this case."""
    to: str = Field(pattern=TARGET)
    ask: AskSpec | None = None


class BranchSpec(FolderModel):
    """A conditional edge: look at one enum output field, pick a target."""

    from_: str = Field(alias="from", pattern=NODE_NAME)

    route_on: str = Field(pattern=FIELD_NAME)
    """Which of the node's output fields to switch on. The validator requires
    it to be declared with type "enum", which is what lets it also prove that
    every possible value has somewhere to go."""

    cases: list[CaseSpec] = Field(min_length=1, max_length=8)
    default: str = Field(default=END_TARGET, pattern=TARGET)


class GraphFile(FolderModel):
    """graph.json."""

    format_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    entry: str = Field(pattern=NODE_NAME)
    nodes: list[NodeRef] = Field(min_length=1, max_length=48)
    """48: the planner's largest plan -- four stages of eight substeps -- is
    32 work nodes, which with no step ever merged need eight checkers and a
    review node. 40 before that, when a real design wanted 34; 30 was raised
    from 20 -- see BranchSpec below for the arithmetic that
    made 20 too small once every stage grew a verifier."""
    edges: list[EdgeSpec] = Field(default_factory=list)
    branches: list[BranchSpec] = Field(default_factory=list, max_length=16)
    """16, raised from 8, and the two 8s were easy to confuse: `cases` above
    caps the fan-out of ONE branch, this capped how many branches a graph may
    have at all.

    8 branches meant at most 8 checked stages, because the design pattern the
    designer is now told to use spends one branch per stage (worker -> checker
    -> branch). A 13-step plan needs 13, and what actually happened was worse
    than a clear refusal: the designer emitted 20 node names, no edges, no
    branches and one node entry -- an answer shaped like the graph it could
    not express -- and the human got 42 validation errors describing the
    wreckage rather than the cause."""


# ---------------------------------------------------------------------------
# nodes.json -- per-node configuration
# ---------------------------------------------------------------------------

class FieldSpec(FolderModel):
    """One field of a node's structured output.

    outputs.py turns a list of these into a real Pydantic model, whose JSON
    Schema is what constrains the provider's reply.
    """

    name: str = Field(pattern=FIELD_NAME)
    type: Literal["string", "enum", "string_list", "integer", "boolean"]
    choices: list[str] = Field(default_factory=list, max_length=8)
    """Required for type "enum", ignored otherwise."""
    description: str = ""
    required: bool = False


class PromptPair(FolderModel):
    """The two prompts a node can send.

    THE ONE RULE: `first` is used when this node has no provider conversation
    yet, OR when the counter named in `refresh_on` has changed since this
    conversation last ran. Otherwise `next`.

    That single rule reproduces both of today's behaviours -- the discussor's
    "no thread yet" branch and the executor's "the plan was revised, resend it"
    branch.
    """

    first: str
    next: str = ""
    """Empty means "reuse first"."""


class CommandSpec(FolderModel):
    """One slash command a human node accepts."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,15}$")
    to: str = Field(pattern=TARGET)
    purposes: list[str] | None = None
    """Which ask-purposes this command is valid in. None = everywhere."""
    aliases: list[str] = Field(default_factory=list, max_length=4)
    argument: str = ""
    summary: str = ""
    record: str = ""
    """Optional transcript line to append when this command is used."""
    sets: dict[str, str] = Field(default_factory=dict)
    """Values to write into `vars`, readable later as {var.<key>}."""
    outcome: str = ""
    """Optional run outcome to record, e.g. "new_task"."""


class SkillChoice(FolderModel):
    """Which shared skills (<repo>/skills, agent/skills) a node gets.

    none -- none; auto -- it is shown every skill's name and description and
    reads the ones it needs; selected -- the skills named here, in full."""

    mode: Literal["none", "auto", "selected"] = "none"
    skills: list[str] = Field(default_factory=list, max_length=20)


class AgentNodeConfig(FolderModel):
    """Config for a kind: "agent" node -- one structured model turn."""

    backend: str = Field(min_length=1, max_length=64)
    """A ROLE NAME, fed straight through config.backend_for(). So a generated
    node with backend "reviewer" is configurable with
    --backend-role reviewer=codex:gpt-5.6-sol, using the existing config
    stack."""

    access: Literal["none", "read_only", "write"] = "read_only"

    steps: list[str] = Field(default_factory=list, max_length=20)
    """Plan step ids this node is responsible for, e.g. ["2", "3.1"].

    The context-control mechanism: {my_steps} in this node's prompt renders
    only these, in full, while {plan_outline} gives one line per step so the
    node knows where it sits. Without this every node got the whole brief and
    tried to do the whole job."""

    instructions: str = ""
    output: list[FieldSpec] = Field(min_length=1, max_length=12)
    prompts: PromptPair

    thread_key: str = ""
    """Renders to a suffix for the provider-conversation key. Empty means one
    conversation for this node. The executor uses "{out.planner.workstream}" to
    get one conversation per workstream, which is today's behaviour."""

    refresh_on: str = ""
    """Name of a counter. When it differs from the value recorded the last time
    this conversation ran, use prompts.first instead of prompts.next."""

    bump: list[str] = Field(default_factory=list, max_length=4)
    """Counters to increment after this node runs."""

    capture: list[Literal["git_status", "git_diff", "turn_changes"]] = Field(
        default_factory=list)
    """git_status / git_diff: the repository as it stands after the turn.
    turn_changes: only the files THIS turn changed -- what a checker needs to
    see whether a worker stayed inside its step."""
    record: str = ""
    announce: str = ""
    skills: SkillChoice | None = None
    """Added to its first prompt of a conversation. None: no skills -- what
    every agent designed before skills had."""


class HumanNodeConfig(FolderModel):
    """Config for a kind: "human" node.

    Only the commands are configurable. Routing is deliberately NOT: plain text
    goes to whoever asked, a command goes to that command's `to`, and an
    unrecognised command re-asks. Making that configurable would let a folder
    produce a human node you cannot escape from.
    """

    commands: list[CommandSpec] = Field(default_factory=list, max_length=12)


class OrchestratorNodeConfig(FolderModel):
    """Config for a kind: "orchestrator" node -- the hub of a hub agent.

    A hub agent has no branches. Every work node, the validator and the paper
    node have one edge, back to the orchestrator; the orchestrator decides
    each time where the run goes next -- a node to do work, the validator to
    check it, the human to ask, or the end. That is what lets a failure found
    in step 3 go back to step 1, where it started, rather than only to the
    step that was just checked. agent/work/templates/orchestrator_node.py.

    Every setting has a default; a designed agent writes only the wiring.
    """

    backend: str = Field(default="orchestrator", min_length=1, max_length=64)
    """A role name. "orchestrator" runs where the discussor does unless it is
    configured itself (roles.ROLE_ALIASES), so it can continue the
    discussor's own conversation."""

    instructions: str = ""
    """Extra, experiment-specific instructions. The orchestrator's rules
    themselves come from code."""

    validator: str = Field(pattern=NODE_NAME)
    """The one node it sends work to be checked."""
    paper: str = Field(default="", pattern=r"^([a-z][a-z0-9_]{0,31})?$")
    """The node that writes the paper; the run does not finish before it has
    written one that passed validation. Empty: no paper."""
    review: str = Field(pattern=NODE_NAME)
    """The human node it asks: a gate, a question it cannot settle, the
    decision at the end of the run."""

    # ---- loop guards: hitting one asks the human ---------------------------
    max_turns: int = Field(default=80, ge=1, le=2000)
    """Decisions it may make in one run."""
    max_visits: int = Field(default=8, ge=1, le=200)
    """Times one node may be sent work in one run."""
    max_failures: int = Field(default=3, ge=1, le=50)
    """Validations one node may fail in a row before a person is asked."""

    # ---- context -------------------------------------------------------------
    roll_percent: int = Field(default=70, ge=10, le=95)
    """When its conversation fills this much of the model's context window,
    the next turn starts a fresh one, seeded from its memory."""
    context_window: int = Field(default=0, ge=0)
    """Tokens. 0: the provider's own figure, else backends/models.py's."""

    announce: str = ""
    skills: SkillChoice | None = None
    """Added to the briefing it gets with each new conversation."""


NodeConfig = AgentNodeConfig | HumanNodeConfig | OrchestratorNodeConfig


# ---------------------------------------------------------------------------
# How a node is written back to nodes.json
# ---------------------------------------------------------------------------

#: The keys an agent node may carry on disk, in the order they are written.
#: Order matters only for readability -- but readability is the point of a
#: hand-editable format, and a stable order keeps `git diff` on a promoted
#: agent about what actually changed.
AGENT_KEYS: tuple[str, ...] = (
    "backend", "access", "steps", "instructions", "output", "prompts",
    "thread_key", "refresh_on", "bump", "capture", "record", "announce", "skills",
)


def node_entry(kind: str | None, values: dict) -> dict:
    """One nodes.json entry, written the one canonical way.

    TWO callers, and they must not drift: the design phase writing a folder the
    model just invented (agent/bootstrap/nodes/writer.py) and the web editor
    saving one you changed by hand (webui/editing.py). When they disagreed, a
    no-op save through the editor rewrote every file -- which turns `git diff`
    on a promoted agent into noise and hides the edit you actually made.

    Two rules, both about being readable rather than complete:

      - Empty values are dropped. An agent node carrying `"record": ""` and
        `"bump": []` validates perfectly and is miserable to read.
      - `access` is kept even at its default. It is the one field whose value
        has consequences you want to see without thinking -- "does this node
        touch my files?" should never require knowing what the default is.
    """
    if kind == "human":
        return {"commands": [command_entry(c) for c in values.get("commands", [])]}
    if kind == "orchestrator":
        # Wiring always, settings only where they differ from the default --
        # a hub agent's file then shows exactly what was tuned.
        defaults = OrchestratorNodeConfig(validator="x", review="x").model_dump()
        out = {}
        for key in ORCHESTRATOR_KEYS:
            value = values.get(key)
            if key in ("validator", "review"):
                out[key] = value
            elif value not in (None, "", [], {}) and value != defaults.get(key):
                out[key] = value
        return out

    out: dict = {}
    for key in AGENT_KEYS:
        value = values.get(key)
        if key == "access":
            # Written in POSITION, not appended. A setdefault() after the loop
            # put it last, so a no-op save reordered every agent node in the
            # file -- correct JSON, unreadable diff.
            out["access"] = value or "read_only"
            continue
        if value in (None, "", [], {}):
            continue
        if key == "output":
            out[key] = [_field_entry(f) for f in value]
        elif key == "prompts":
            out[key] = {k: v for k, v in value.items() if v}
        else:
            out[key] = value
    return out


#: An orchestrator node's keys, in the order they are written.
ORCHESTRATOR_KEYS: tuple[str, ...] = (
    "backend", "instructions", "validator", "paper", "review", "max_turns",
    "max_visits", "max_failures", "roll_percent", "context_window", "announce",
    "skills",
)


#: One output field's keys, in the order they are written.
FIELD_KEYS: tuple[str, ...] = ("name", "type", "choices", "description", "required")


def _field_entry(values: dict) -> dict:
    """One entry of a node's `output` list.

    Cleaned HERE rather than left to the caller's model_dump, so the two
    writers cannot disagree by dumping differently. The design phase gets its
    fields from a model whose strict-output schema requires every property, so
    they arrive with `"choices": [], "description": "", "required": false`
    spelled out; the editor's arrive already trimmed. Same file either way.
    """
    out = {}
    for key in FIELD_KEYS:
        value = values.get(key)
        if key in ("name", "type"):
            out[key] = value
        elif value not in (None, "", [], {}, False):
            out[key] = value
    return out


#: A command's keys, in the order they are written.
COMMAND_KEYS: tuple[str, ...] = (
    "name", "to", "purposes", "aliases", "argument", "summary", "record",
    "sets", "outcome",
)


def command_entry(values: dict) -> dict:
    """One human-node command, written the one canonical way.

    Same empty-dropping rule as `node_entry`, and for the same reason: a
    command carrying `"record": "", "sets": {}, "outcome": ""` validates fine
    and is three lines of noise per command in a file you are meant to read.

    `purposes` is the interesting one. `None` means "legal at every question",
    and that is the DEFAULT -- so dropping it is not just tidiness, it is the
    difference between a file that says what it means and one that spells out
    the absence of a restriction.
    """
    return {key: values[key] for key in COMMAND_KEYS
            if values.get(key) not in (None, "", [], {})}


#: graph.json's keys, in the order they are written.
GRAPH_KEYS: tuple[str, ...] = (
    "name", "description", "entry", "nodes", "edges", "branches",
)


def graph_document(values: dict) -> dict:
    """graph.json, written the one canonical way.

    `format_version` is injected here rather than taken from the input: it
    describes the FILE, and neither a model proposing an agent nor a browser
    editing one has any business choosing it.

    Everything else follows node_entry's rule -- drop what is empty -- so a
    graph with no branches simply has no `branches` key rather than an empty
    list to read past. The loader defaults it back.
    """
    out: dict = {"format_version": 1}
    for key in GRAPH_KEYS:
        value = values.get(key)
        if key in ("name", "entry", "nodes"):
            out[key] = value          # required; write it even if it is odd
        elif value not in (None, "", [], {}):
            out[key] = value

    # A branch's `default` is spelled out even when it is the default value,
    # for the same reason `access` is: "where does this go when nothing
    # matches?" is a question you should be able to answer by looking, not by
    # remembering what the fallback is. Everything else nested inside a branch
    # follows the ordinary drop-what-is-empty rule.
    for branch in out.get("branches", []):
        branch.setdefault("default", END_TARGET)

    return out
