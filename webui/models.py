"""
WHAT:  The shapes that cross the HTTP boundary.
WHY:   One place to look when the browser and the server disagree.
CONCEPT: Pydantic models as an API contract.

Note what is NOT here: the agent folder's own models. `graph.json` and
`nodes.json` are already Pydantic (agent/agentfolder/schema.py), and copying
them into a second set of "API" models is precisely how the reference project
we drew on ended up with two schema definitions a whole version apart. So the
folder endpoints pass the real models through, and `GET /api/schema` serves
their generated JSON Schema so the browser builds forms from the truth.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    """Reject unknown keys, like everything else in this project.

    Worth it on request bodies specifically: a typo'd field would otherwise be
    silently ignored, and "I set it and nothing happened" is a much worse
    afternoon than "422, unknown field".
    """

    model_config = ConfigDict(extra="forbid")


# ---- creating and listing sessions ---------------------------------------


class NewSession(Strict):
    """Mirrors the `run` subcommand's flags, because it does the same job."""

    repo: str = "."
    session: str | None = None
    session_dir: str | None = None
    sessions_root: str | None = None
    """Same as --sessions-root: the folder session folders are made in."""
    task: str = ""
    pre_build_agent: str | None = None

    # Backend overrides, same meaning as --backend / --model / --backend-role.
    backend: str | None = None
    model: str | None = None
    backend_role: list[str] = Field(default_factory=list)

    delegate: list[str] | None = None
    """ROLE=PROVIDER[:MODEL]: where a role's turns go when its own provider is
    out of credits -- only for roles listed. See agent/backends/delegate.py."""
    role_options: dict[str, dict[str, bool]] | None = None
    """Backend options per role, as the Runs on row sets them -- today only
    {"<role>": {"gpu": true}}: run a Codex role without its sandbox."""
    research: bool | None = None
    """Let the discussor ask the research agent before it suggests answers
    (the New dialog's checkbox). None: the config decides (on by default)."""

    # Same meaning as --supervisor / --max-iterations. None: the config decides.
    supervisor: Literal["manual", "auto", "off"] | None = None
    max_iterations: int | None = Field(default=None, ge=1, le=20)

    start: bool = True
    """Begin running immediately. False just resolves the folder, which is what
    you want when opening a session to look at its graph."""


class SessionSummary(Strict):
    id: str
    name: str
    folder: str
    task: str = ""
    has_agent: bool = False
    phase: str = "idle"
    open: bool = False
    """True when the server holds a live runner for it -- i.e. it has a
    checkpointer open and can be answered."""


class SessionDetail(SessionSummary):
    repo: str = ""
    checkpoint: str = ""
    artifacts: str = ""
    agent_name: str = ""
    agent_path: str = ""
    busy: bool = False
    waiting: bool = False
    pending: dict[str, Any] | None = None
    commands: list[dict[str, Any]] = Field(default_factory=list)
    """The slash commands valid at the pending question -- the buttons under
    it. Here as well as on the question event: the page redraws the question
    from this on every refresh, and without them it hid the buttons."""
    last_seq: int = 0
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    """The conversation so far, from the CHECKPOINT rather than the event bus.

    The bus is an in-memory ring per server process, so restarting emptied the
    browser's chat history while the session itself still had all of it. The
    conversation that produced an agent is not a log -- it is the thing
    somebody comes back to read."""
    error: str = ""
    problems: list[dict[str, Any]] = Field(default_factory=list)


# ---- driving a session ----------------------------------------------------


class Answer(Strict):
    """What the human typed. A bare string, exactly as the terminal sends it.

    `agent/commands.py` re-parses it inside the graph, so slash commands need no
    special handling here -- "/plan" is just text.
    """

    text: str


class StartRun(Strict):
    task: str = ""
    pre_build_agent: str | None = None


# ---- responses ------------------------------------------------------------


class ProblemOut(Strict):
    """`agent.agentfolder.validate.Problem`, on the wire.

    Reused as the API's error envelope too, so a validation failure and a bad
    request have one shape between them.
    """

    code: str
    where: str
    message: str
    warning: bool = False


class DeleteSession(Strict):
    """Which session to delete when the server does not have it open."""

    repo: str = "."
    sessions_root: str | None = None
    drop_branch: bool = False
    """Also delete its git branch -- the code it wrote. Kept by default."""


class Ok(Strict):
    ok: bool = True
    detail: str = ""
