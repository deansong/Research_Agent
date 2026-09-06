"""
WHAT:  The HTTP surface: routes in, JSON and SSE out.
WHY:   Somewhere has to turn requests into calls on a SessionRunner. This file
       does only that.
CONCEPT: FastAPI, plus one streaming endpoint.

--------------------------------------------------------------------------
WHERE THE PIECES LIVE
--------------------------------------------------------------------------
    server.py    routes; no graph logic, no threading
    runner.py    one worker thread per session -- the web's drive()
    events.py    the event vocabulary and the replay buffer
    capture.py   how a node's print() reaches the browser
    editing.py   reading and writing plan.json and the agent folder

If you are reading to understand how a run WORKS, read runner.py. This file is
the boring half on purpose.

--------------------------------------------------------------------------
STATE, AND WHY THE SERVER HOLDS ANY
--------------------------------------------------------------------------
A REST purist would want this stateless: every request opens the checkpointer,
resumes, closes. That is nearly workable -- LangGraph checkpoints everything --
but it breaks on the thing that matters most here: a Codex turn takes minutes
and we want to watch it. Watching means a worker thread and a live event
stream, and those cannot be reconstructed per request.

So `_RUNNERS` maps a session id to a live SessionRunner. It is process-local
and deliberately not persisted: everything that MATTERS is already on disk in
the session folder, so losing this dict costs you an open connection, not work.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent import storage
from agent.agentfolder.load import AgentFolderError, load_agent_folder
from agent.agentfolder.schema import GraphFile
from agent.agentfolder.validate import Problem, validate_folder
from agent.backends import PROVIDERS
from agent.backends.base import Access
from agent.config import backend_for, load_config
from agent.runtime import needed_for
from webui import editing
from webui.models import (
    Answer,
    NewSession,
    Ok,
    ProblemOut,
    SessionDetail,
    SessionSummary,
    StartRun,
)
from webui.runner import SessionRunner

STATIC = Path(__file__).resolve().parent / "static"

#: session id -> live runner. See the module docstring on why this exists.
_RUNNERS: dict[str, SessionRunner] = {}
_RUNNERS_LOCK = threading.Lock()

#: Defaults for this server process, set by `create_app`. They stand in for the
#: command-line flags the CLI would have read.
_DEFAULTS: dict = {"repo": Path("."), "cli_args": None}


def create_app(repo: Path, cli_args=None) -> FastAPI:
    """Build the app. `cli_args` is the argparse namespace from `main.py web`,
    so --backend and friends mean the same thing here as they do for `run`."""
    _DEFAULTS["repo"] = Path(repo).resolve()
    _DEFAULTS["cli_args"] = cli_args

    app = FastAPI(title="Agent designer", version="1", lifespan=_lifespan)
    _register(app)

    # Mounted last so /api/* wins. html=True serves index.html at "/".
    app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")

    return app


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Close every open session's checkpointer when the server stops.

    Without this a Ctrl-C leaves SQLite connections and a Codex client dangling
    until the process is reaped. Nothing is LOST -- the checkpoint is committed
    after every completed node -- but it is untidy, and on Windows it keeps the
    database file locked.
    """
    yield
    with _RUNNERS_LOCK:
        runners = list(_RUNNERS.values())
        _RUNNERS.clear()
    for runner in runners:
        runner.shutdown()


def _register(app: FastAPI) -> None:
    # ---- sessions --------------------------------------------------------

    @app.get("/api/sessions", response_model=list[SessionSummary])
    def list_sessions(repo: str | None = None):
        """Every session on disk, plus whether the server has one open."""
        root = Path(repo).resolve() if repo else _DEFAULTS["repo"]
        out = []
        for name, task, has_agent in storage.list_sessions(root):
            runner = _RUNNERS.get(name)
            out.append(SessionSummary(
                id=name, name=name,
                folder=str(root / storage.DOT_AGENT / "sessions" / name),
                task=task, has_agent=has_agent,
                phase=runner.phase if runner else "idle",
                open=runner is not None,
            ))
        return out

    @app.post("/api/sessions", response_model=SessionDetail)
    def create_session(body: NewSession):
        """Resolve (or create) a session folder and open a runner on it.

        The same three routes in the CLI has: an explicit folder, a named
        session, or a name derived from the task.
        """
        repo = Path(body.repo).expanduser().resolve() if body.repo else _DEFAULTS["repo"]
        if not repo.is_dir():
            raise _bad("bad_repo", str(repo), "Not a directory.")

        cfg = load_config(repo_path=repo, cli=_Overrides(body, _DEFAULTS["cli_args"]))

        task = body.task.strip()
        if body.session_dir or body.session:
            paths = storage.session_paths(
                repo, body.session or "unnamed", session_dir=body.session_dir
            )
            task = task or _saved_task(paths)
        else:
            if not task:
                raise _bad("no_task", "task",
                           "Supply a task, a session name, or a session folder.")
            paths = storage.session_paths(repo, storage.session_name_for(task))

        if not task:
            raise _bad("no_task", str(paths.session),
                       "That session has no saved task; supply one.")
        paths.request.write_text(task)

        runner = _open_runner(paths.session.name, cfg, paths)
        if body.start and not runner.busy:
            runner.start(task, pre_build_agent=body.pre_build_agent)
        return _detail(runner, task)

    @app.get("/api/sessions/{session_id}", response_model=SessionDetail)
    def get_session(session_id: str):
        return _detail(_runner(session_id))

    @app.post("/api/sessions/{session_id}/start", response_model=SessionDetail)
    def start_session(session_id: str, body: StartRun = Body(default=StartRun())):
        runner = _runner(session_id)
        if runner.busy:
            raise _bad("already_running", session_id, "This session is already running.")
        runner.start(body.task or _saved_task(runner.paths),
                     pre_build_agent=body.pre_build_agent)
        return _detail(runner)

    @app.post("/api/sessions/{session_id}/answer", response_model=Ok)
    def answer(session_id: str, body: Answer):
        """Hand the human's reply to the waiting worker.

        409 rather than 400 when nothing is waiting: it is a state conflict, not
        a malformed request, and the browser retries differently for each.
        """
        runner = _runner(session_id)
        try:
            runner.answer(body.text)
        except RuntimeError as exc:
            raise _bad("not_waiting", session_id, str(exc), status=409)
        return Ok(detail="delivered")

    @app.delete("/api/sessions/{session_id}", response_model=Ok)
    def close_session(session_id: str):
        """Close the runner. The session folder is untouched -- everything that
        matters is on disk, so this only frees the checkpointer and the thread."""
        with _RUNNERS_LOCK:
            runner = _RUNNERS.pop(session_id, None)
        if runner is None:
            raise _bad("no_session", session_id, "No open session with that id.",
                       status=404)
        runner.shutdown()
        return Ok(detail="closed")

    # ---- the plan --------------------------------------------------------

    @app.get("/api/sessions/{session_id}/plan")
    def get_plan(session_id: str):
        runner = _runner(session_id)
        plan = editing.read_plan(runner.paths)
        return {"plan": plan, "path": str(runner.paths.plan),
                "exists": runner.paths.plan.exists(),
                "step_ids": sorted(editing.plan_step_ids(plan))}

    @app.put("/api/sessions/{session_id}/plan")
    def put_plan(session_id: str, body: dict = Body(...)):
        """Save an edited plan.

        Lands on a hook that already existed: the planner writes plan.json
        BEFORE asking for approval, and bootstrap/nodes/human.py re-reads it
        from disk when you type /approve. So editing here and then approving is
        the supported path, not a workaround.
        """
        runner = _runner(session_id)
        problems = editing.write_plan(runner.paths, body.get("plan", body))
        blocking = [p for p in problems if not p.warning]
        if blocking:
            raise _bad("bad_plan", str(runner.paths.plan), blocking[0].message, 422)
        return {"saved": True, "problems": [_problem(p) for p in problems]}

    # ---- the agent folder ------------------------------------------------

    @app.get("/api/sessions/{session_id}/agent")
    def get_agent(session_id: str):
        """graph.json and nodes.json as one document, plus a drawable view."""
        runner = _runner(session_id)
        path = _agent_path(runner)
        try:
            document = editing.read_folder(path)
            folder = load_agent_folder(path)
        except AgentFolderError as exc:
            raise _bad("unreadable_agent", str(path), str(exc), 422)

        plan = editing.read_plan(runner.paths)
        return {
            **document,
            "editable": path == runner.paths.agent_dir,
            "view": editing.graph_view(folder, plan),
            "problems": [_problem(p) for p in validate_folder(folder)],
        }

    @app.put("/api/sessions/{session_id}/agent")
    def put_agent(session_id: str, body: dict = Body(...)):
        """Save an edited agent.

        Saving is allowed even when the result has problems -- you cannot
        rewire a graph without passing through invalid states -- so the response
        always carries the validation report and the UI decides how loudly to
        say it. Running is what is gated, by cli.py and by the runner.
        """
        runner = _runner(session_id)
        if runner.busy:
            raise _bad("running", session_id,
                       "Stop the run before editing its agent.", 409)

        path = _agent_path(runner)
        if path != runner.paths.agent_dir:
            raise _bad("not_editable", str(path),
                       "That agent is shipped or promoted, not this session's own. "
                       "Copy it into the session first.", 409)

        result = editing.save_folder(path, body, staging=runner.paths.staging)
        if not result.saved:
            raise _bad("invalid_agent", str(path), result.error, 422)
        return {"saved": True, "problems": [_problem(p) for p in result.problems]}

    @app.post("/api/sessions/{session_id}/agent/validate")
    def validate_agent(session_id: str, body: dict = Body(...)):
        """Check an edit without writing it -- for live feedback while typing."""
        _runner(session_id)
        try:
            graph = GraphFile.model_validate({"format_version": 1, **body.get("graph", {})})
        except Exception as exc:  # pydantic.ValidationError
            return {"ok": False, "problems": [{
                "code": "invalid_graph", "where": "graph.json",
                "message": editing._pydantic_message(exc), "warning": False}]}

        with tempfile.TemporaryDirectory() as tmp:
            result = editing.save_folder(
                Path(tmp) / "agent", {"graph": graph.model_dump(mode="json", by_alias=True),
                                      "nodes": body.get("nodes", {})},
                staging=Path(tmp) / "staging",
            )
        if not result.saved:
            return {"ok": False, "problems": [{
                "code": "invalid_agent", "where": "nodes.json",
                "message": result.error, "warning": False}]}
        return {"ok": not [p for p in result.problems if not p.warning],
                "problems": [_problem(p) for p in result.problems]}

    @app.get("/api/sessions/{session_id}/nodes/{name}/context")
    def get_node_context(session_id: str, name: str):
        """What this node will actually be sent, rendered against live state.

        The most useful screen in the UI, because a template and the prompt it
        produces are very different things -- and an unresolved placeholder
        renders as empty rather than raising, so a wrong one looks fine
        everywhere except here.
        """
        runner = _runner(session_id)
        path = _agent_path(runner)
        try:
            folder = load_agent_folder(path)
        except AgentFolderError as exc:
            raise _bad("unreadable_agent", str(path), str(exc), 422)

        state = dict(runner.last_values or {})
        state.setdefault("plan", editing.read_plan(runner.paths))
        state.setdefault("task_brief", _saved_task(runner.paths))
        state.setdefault("repo_path", str(runner.paths.repo))
        state.setdefault("artifacts_dir", str(runner.paths.artifacts))

        try:
            return editing.node_context(
                folder, name, state=state,
                artifacts_dir=str(runner.paths.artifacts),
                session_dir=str(runner.paths.session),
            )
        except KeyError:
            raise _bad("no_node", name, f"No node named {name!r} in this agent.", 404)

    # ---- the event stream ------------------------------------------------

    @app.get("/api/sessions/{session_id}/events")
    def events(
        session_id: str,
        request: Request,
        after_seq: int = Query(0, ge=0),
        last_event_id: str | None = None,
    ):
        """Server-Sent Events: replay, then follow.

        `Last-Event-ID` is sent by the browser's EventSource automatically on a
        reconnect, so honouring it is what makes a dropped connection lossless.
        `?after_seq=` is the manual equivalent, for a client that reconnects on
        purpose. See webui/events.py for the frame format.
        """
        runner = _runner(session_id)
        header = request.headers.get("last-event-id") or last_event_id
        start = after_seq
        if header and header.isdigit():
            start = max(start, int(header))

        return StreamingResponse(
            _stream(runner, request, start),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # nginx buffers text/event-stream by default, which turns a live
                # feed into one giant delivery at the end. Harmless locally,
                # baffling behind a proxy.
                "X-Accel-Buffering": "no",
            },
        )

    # ---- describing the system ------------------------------------------

    @app.get("/api/config")
    def config(repo: str | None = None):
        """The resolved backend for each role the open sessions care about."""
        root = Path(repo).resolve() if repo else _DEFAULTS["repo"]
        cfg = load_config(repo_path=root, cli=_DEFAULTS["cli_args"])
        roles = sorted({*cfg.roles, "discussor", "planner", "designer"})
        return {
            "default": {"provider": cfg.default.provider, "model": cfg.default.model},
            "roles": {
                role: {
                    "provider": backend_for(cfg, role).provider,
                    "model": backend_for(cfg, role).model,
                    "configured": role in cfg.roles,
                }
                for role in roles
            },
            "sources": list(cfg.sources),
            "recursion_limit": cfg.recursion_limit,
            "work_recursion_limit": cfg.work_recursion_limit,
        }

    @app.get("/api/schema")
    def schema():
        """Everything the browser needs to build a form, from the real models.

        Served rather than hardcoded in JS on purpose. The folder schema is
        Pydantic with `extra="forbid"`; a front end carrying its own copy of the
        field list drifts the first time a field is added, and then silently
        posts bodies the server rejects.
        """
        return {
            "graph": GraphFile.model_json_schema(),
            "node_kinds": ["agent", "human"],
            "access_levels": [a.value for a in Access],
            # A node may only ask for these three; "full" is a backend
            # capability, not something a folder can request.
            "node_access_levels": ["none", "read_only", "write"],
            "field_types": ["string", "enum", "string_list", "integer", "boolean"],
            "providers": sorted(PROVIDERS),
            "end_target": "__end__",
        }

    @app.get("/api/health")
    def health():
        return {"ok": True, "open_sessions": sorted(_RUNNERS)}


# ---- streaming ------------------------------------------------------------


#: How often the stream checks for new events. Latency the human never notices,
#: next to a model turn measured in minutes.
POLL_SECONDS = 0.25

#: A comment frame every few seconds. EventSource ignores lines starting with
#: ':', but proxies and load balancers count them as traffic and so leave an
#: idle connection alone.
PING_SECONDS = 15.0


async def _stream(runner: SessionRunner, request: Request,
                  after_seq: int) -> AsyncIterator[bytes]:
    """Yield SSE frames until the client goes away or the session ends.

    ASYNC, and that is load-bearing rather than stylistic. The first version was
    a sync generator blocking on a condition variable, and it deadlocked
    shutdown: uvicorn waits for open connections before running lifespan
    shutdown, the connections were blocked waiting for events, and the only
    thing that would have released them was the shutdown. A sync generator in a
    threadpool also cannot be cancelled when a browser tab closes.

    An async loop is cancellable at every await, and can ask Starlette whether
    the client is still there.
    """
    gap = runner.bus.first_seq
    if after_seq and gap and after_seq < gap - 1:
        # The client asked for events we have already dropped. Say so rather
        # than resuming with a silent hole in the middle of the run.
        yield b": history truncated; reload for the full transcript\n\n"
        after_seq = 0

    yield f": stream open at seq {after_seq}\n\n".encode()

    cursor = after_seq
    last_ping = time.monotonic()

    while True:
        if await request.is_disconnected():
            return

        pending = runner.bus.replay(cursor)
        if pending:
            for event in pending:
                cursor = event.seq
                yield event.sse().encode()
            last_ping = time.monotonic()
            continue

        # Caught up. Stop only once the session is finished AND we have sent
        # everything -- checked in this order so a `done` event emitted while we
        # were mid-yield is never left unsent.
        if runner.bus.closed:
            return

        if time.monotonic() - last_ping > PING_SECONDS:
            yield b": ping\n\n"
            last_ping = time.monotonic()

        await asyncio.sleep(POLL_SECONDS)


# ---- helpers --------------------------------------------------------------


class _Overrides:
    """Presents an HTTP body to `load_config` as if it were argparse.

    `load_config(cli=...)` reads attributes off an argparse Namespace. Rather
    than teach it about a second input shape, we give it something with the
    same attributes -- falling back to the flags this server was started with,
    so `main.py web . --backend fake` applies to every session.
    """

    def __init__(self, body: NewSession, defaults):
        self._body = body
        self._defaults = defaults

    def __getattr__(self, name: str):
        value = getattr(self._body, name, None)
        if value in (None, [], ""):
            return getattr(self._defaults, name, None)
        return value


def _open_runner(session_id: str, cfg, paths) -> SessionRunner:
    with _RUNNERS_LOCK:
        runner = _RUNNERS.get(session_id)
        if runner is None:
            runner = SessionRunner(session_id, cfg, paths)
            _RUNNERS[session_id] = runner
        return runner


def _runner(session_id: str) -> SessionRunner:
    runner = _RUNNERS.get(session_id)
    if runner is None:
        raise _bad("no_session", session_id,
                   "No open session with that id. POST /api/sessions first.",
                   status=404)
    return runner


def _problem(problem: Problem) -> dict:
    """One Problem on the wire. Same shape as an API error, on purpose."""
    return ProblemOut(code=problem.code, where=problem.where,
                      message=problem.message, warning=problem.warning).model_dump()


def _agent_path(runner: SessionRunner) -> Path:
    """Which folder this session is looking at.

    `runner.folder_path` is set once a run has resolved one -- which may be a
    shipped or promoted agent rather than the session's own. Falling back to
    the session's agent_dir covers the case where the design phase finished but
    no run has started yet.
    """
    if runner.folder_path is not None:
        return Path(runner.folder_path)
    if runner.paths.has_agent():
        return runner.paths.agent_dir
    raise _bad("no_agent", runner.id,
               "This session has no agent yet. Finish the design phase first.", 404)


def _saved_task(paths) -> str:
    """The task this session already knows about: yours, then ours."""
    for path in (paths.input_brief, paths.request):
        if path.exists():
            text = path.read_text().strip()
            if text:
                return text
    return ""


def _detail(runner: SessionRunner, task: str = "") -> SessionDetail:
    paths = runner.paths
    agent_name, agent_path, problems = "", "", []

    folder_path = runner.folder_path or (paths.agent_dir if paths.has_agent() else None)
    if folder_path is not None:
        loaded = runner.runtime.load_folder(Path(folder_path))
        agent_path = str(folder_path)
        if loaded.folder is not None:
            agent_name = loaded.folder.graph.name
            problems = [ProblemOut(code=p.code, where=p.where, message=p.message,
                                   warning=p.warning).model_dump()
                        for p in loaded.problems]
        else:
            problems = [ProblemOut(code="unreadable", where=agent_path,
                                   message=loaded.error).model_dump()]

    return SessionDetail(
        id=runner.id,
        name=paths.session.name,
        folder=str(paths.session),
        repo=str(paths.repo),
        checkpoint=str(paths.checkpoint),
        artifacts=str(paths.artifacts),
        task=task or _saved_task(paths),
        has_agent=paths.has_agent(),
        phase=runner.phase,
        open=True,
        agent_name=agent_name,
        agent_path=agent_path,
        busy=runner.busy,
        waiting=runner.waiting,
        pending=runner.pending.as_dict() if runner.pending else None,
        last_seq=runner.bus.last_seq,
        error=runner.error,
        problems=problems,
    )


def _bad(code: str, where: str, message: str, status: int = 400) -> HTTPException:
    """One error shape for the whole API -- the same one validation uses.

    Deliberately NOT FastAPI's default `{"detail": ...}`: a validation failure
    and a bad request should look the same to the browser, so it can render
    either with one function.
    """
    return HTTPException(
        status_code=status,
        detail=ProblemOut(code=code, where=where, message=message).model_dump(),
    )


def serve(repo: Path, host: str = "127.0.0.1", port: int = 8420, cli_args=None) -> None:
    """Run the server. Called by `main.py web`."""
    import uvicorn

    app = create_app(repo, cli_args)
    print(f"\nAgent designer UI:  http://{host}:{port}\n")
    uvicorn.run(
        app, host=host, port=port, log_level="warning",
        # Without a bound, Ctrl-C waits for every open connection -- and an SSE
        # stream is open by definition. Five seconds is long enough for the
        # streams to notice and short enough that Ctrl-C feels like Ctrl-C.
        timeout_graceful_shutdown=5,
    )
