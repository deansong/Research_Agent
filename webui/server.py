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
import json
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent import storage
from agent.agentfolder.load import AgentFolderError, load_agent_folder
from agent.agentfolder.schema import GraphFile
from agent.agentfolder.validate import Problem, validate_folder
from agent.backends import PROVIDERS
from agent.backends import auth as backend_auth
from agent.backends.base import Access
from agent.config import backend_for, load_config
from agent.work.compile import backends_needed as needed_for
from webui import auth as auth_flows
from webui import editing
from webui.models import (
    Answer,
    DeleteSession,
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
_DEFAULTS: dict = {"repo": Path("."), "cli_args": None, "host": "127.0.0.1"}


def create_app(repo: Path, cli_args=None, host: str = "127.0.0.1") -> FastAPI:
    """Build the app. `cli_args` is the argparse namespace from `main.py web`,
    so --backend and friends mean the same thing here as they do for `run`.

    `host` is remembered only so the login routes can refuse to run on a server
    that is reachable from elsewhere. See `_login_allowed`."""
    _DEFAULTS["repo"] = Path(repo).resolve()
    _DEFAULTS["cli_args"] = cli_args
    _DEFAULTS["host"] = host

    app = FastAPI(title="Agent designer", version="1", lifespan=_lifespan)
    _register(app)

    # Mounted last so /api/* wins. html=True serves index.html at "/".
    app.mount("/", _NoCacheStatic(directory=str(STATIC), html=True), name="static")

    return app


class _NoCacheStatic(StaticFiles):
    """Serve the UI with caching switched off.

    There is no build step here, so no content hashes in filenames -- app.js is
    always app.js. Browsers then revalidate on their own schedule, and you can
    end up running a NEW index.html against a CACHED app.js. The symptom is a
    page that renders perfectly and responds to nothing, which is
    indistinguishable from a bug in the code and was in fact reported as one.

    A local development tool has nothing to gain from caching and everything
    to lose, so: no-store, and a reload is always a reload.
    """

    def is_not_modified(self, response_headers, request_headers) -> bool:
        # Refuse to answer 304, which is the other half of the same problem.
        return False

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response


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
        runner.shutdown(server_exit=True)
    # A login flow holds a pty and a child process. Neither should outlive the
    # server that started it.
    auth_flows.shutdown()


def _register(app: FastAPI) -> None:
    # ---- sessions --------------------------------------------------------

    @app.get("/api/sessions", response_model=list[SessionSummary])
    def list_sessions(repo: str | None = None, root: str | None = None):
        """Every session under the sessions root (`root`, else the configured
        one), newest first, plus whether the server has it open."""
        repo_path = _resolve_repo(repo)
        folder = _sessions_root(repo_path, root)
        out = []
        for name, task, has_agent in storage.list_sessions(repo_path, folder):
            runner = _RUNNERS.get(name)
            out.append(SessionSummary(
                id=name, name=name,
                folder=str(folder / name),
                task=task, has_agent=has_agent,
                phase=runner.phase if runner else "idle",
                open=runner is not None,
            ))
        # Plus every session the server has open from somewhere else -- one
        # resumed after a restart from the old sessions folder, say. Listing
        # only the chosen folder left those running where Open could not
        # show them.
        listed = {entry.id for entry in out}
        with _RUNNERS_LOCK:
            elsewhere = [r for name, r in _RUNNERS.items() if name not in listed]
        for runner in elsewhere:
            task = (_saved_task(runner.paths).splitlines() or [""])[0]
            out.append(SessionSummary(
                id=runner.id, name=runner.id, folder=str(runner.paths.session),
                task=task, has_agent=runner.paths.has_agent(),
                phase=runner.phase, open=True,
            ))
        return out

    @app.post("/api/sessions", response_model=SessionDetail)
    def create_session(body: NewSession):
        """Resolve (or create) a session folder and open a runner on it.

        The same three routes in the CLI has: an explicit folder, a named
        session, or a new session with a new id.
        """
        # A session the server already has open is that session, wherever its
        # folder is -- not a new, empty one of the same name under whatever
        # sessions folder this request names.
        if body.session and not body.session_dir and body.session in _RUNNERS:
            runner = _RUNNERS[body.session]
            if body.start and not runner.busy:
                runner.start(_saved_task(runner.paths),
                             pre_build_agent=storage.load_options(runner.paths)
                             .get("pre_build_agent"))
            return _detail(runner)

        repo = _resolve_repo(body.repo)
        if not repo.is_dir():
            raise _bad("bad_repo", str(repo), _why_not_a_repo(repo))
        infrastructure = _infrastructure_warning(repo)
        if infrastructure:
            raise _bad("repo_is_infrastructure", str(repo), infrastructure)

        cfg = load_config(repo_path=repo, cli=_Overrides(body, _DEFAULTS["cli_args"]))

        task = body.task.strip()
        root = storage.sessions_root(repo, cfg.sessions_root)
        reopening = bool(body.session_dir or body.session)
        if body.session_dir or body.session:
            paths = storage.session_paths(
                repo, body.session or "unnamed", session_dir=body.session_dir, root=root
            )
            task = task or _saved_task(paths)
        else:
            if not task:
                raise _bad(
                    "no_task", "task",
                    "Say what you want built or changed, in the big box. "
                    "(Or name an existing session to resume instead.)",
                )
            # A new id every time: two sessions never share a folder, however
            # alike their tasks. Resuming is opening one by name, above.
            paths = storage.session_paths(repo, storage.new_session_id(task, root),
                                          root=root)

        if not task:
            raise _bad("no_task", str(paths.session),
                       "That session has no saved task; supply one.")
        paths.request.write_text(task)

        # The choices it was started with, kept with it: reopening a session
        # (or resuming it after a restart) runs it the same way, unless this
        # request says otherwise.
        options = {**(storage.load_options(paths) if reopening else {}),
                   **_options_of(body)}
        if options != _options_of(body):
            body = body.model_copy(update=options)
            cfg = load_config(repo_path=repo, cli=_Overrides(body, _DEFAULTS["cli_args"]))
        if options:
            storage.save_options(paths, options)
        # Its own worktree, if it has not started yet -- see storage.py.
        workspace_note = storage.create_workspace(paths)

        runner = _open_runner(paths.session.name, cfg, paths)
        if workspace_note:
            runner.bus.emit("log", text=workspace_note)
        if body.start and not runner.busy:
            runner.start(task, pre_build_agent=body.pre_build_agent)
        return _detail(runner, task)

    @app.post("/api/sessions/{session_id}/delete", response_model=Ok)
    def delete_session(session_id: str, body: DeleteSession = Body(default=DeleteSession())):
        """Delete a session for good: stop it if it runs, then remove its
        folder, its worktree and its resume mark -- and its branch only if
        asked. A POST with a body rather than a DELETE, because what to delete
        depends on more than the id (which sessions folder, the branch)."""
        runner = _RUNNERS.get(session_id)
        if runner is not None:
            paths = runner.paths
            with _RUNNERS_LOCK:
                _RUNNERS.pop(session_id, None)
            runner.shutdown()
            if runner.busy:
                raise _bad("still_running", session_id,
                           "It is still finishing the turn in flight. Try again in a "
                           "few seconds.", 409)
        else:
            try:
                usable = storage._safe(session_id) == session_id
            except SystemExit:
                usable = False
            if not usable:
                raise _bad("no_session", session_id, "No session with that id.", 404)
            repo = _resolve_repo(body.repo)
            root = _sessions_root(repo, body.sessions_root)
            if not (root / session_id).is_dir():
                raise _bad("no_session", session_id,
                           f"No session {session_id!r} in {root}.", 404)
            paths = storage.session_paths(repo, session_id, root=root)
        try:
            done = storage.delete_session(paths, drop_branch=body.drop_branch)
        except storage.DeleteRefused as exc:
            raise _bad("not_deleted", session_id, str(exc), 409)
        return Ok(detail=" ".join(done))

    @app.get("/api/sessions/{session_id}", response_model=SessionDetail)
    def get_session(session_id: str):
        return _detail(_runner(session_id))

    @app.post("/api/sessions/{session_id}/start", response_model=SessionDetail)
    def start_session(session_id: str, body: StartRun = Body(default=StartRun())):
        runner = _runner(session_id)
        if runner.busy:
            raise _bad("already_running", session_id, "This session is already running.")
        runner.start(body.task or _saved_task(runner.paths),
                     pre_build_agent=body.pre_build_agent
                     or storage.load_options(runner.paths).get("pre_build_agent"))
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

    @app.post("/api/sessions/{session_id}/pause", response_model=Ok)
    def pause(session_id: str):
        """Stop after the current node finishes. Nothing is lost.

        Separate verb from /stop because the trade-off is real and the user
        should get to pick it: this waits for the node in flight, which may be
        minutes, but loses nothing at all.
        """
        return Ok(detail=_runner(session_id).pause())

    @app.post("/api/sessions/{session_id}/stop", response_model=Ok)
    def stop(session_id: str):
        """Stop now, abandoning the turn in flight.

        Reaches the provider, so it takes effect during a long turn -- at the
        cost of that one node's work, since a node is atomic and has no
        half-finished result to keep.
        """
        return Ok(detail=_runner(session_id).stop())

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

    # ---- the design agent (the bootstrap graph) ---------------------------

    @app.get("/api/sessions/{session_id}/bootstrap")
    def get_bootstrap(session_id: str):
        """The agent that designs the agent: every node, its model, its prompts.

        Available from the moment a session is created -- before the first
        turn -- because the point is to look at it, and change it, before it
        interviews you.
        """
        return _bootstrap_view(_runner(session_id))

    @app.put("/api/sessions/{session_id}/bootstrap")
    def put_bootstrap(session_id: str, body: dict = Body(...)):
        """Save changes to the design agent. Applied at the next Start.

        Refused while the session is running: the design graph is built when a
        run starts, so an edit made mid-turn would look saved and do nothing
        until the next start. Pause first; Start picks the change up.
        """
        from agent.bootstrap import settings

        runner = _runner(session_id)
        if runner.busy:
            raise _bad("running", session_id,
                       "Pause the session before changing the design agent. "
                       "Changes apply when it starts again.", 409)
        try:
            settings.save(runner.paths.session, runner.cfg, body)
        except settings.SettingsError as exc:
            raise _bad("bad_bootstrap", exc.where, exc.message, 422)
        return _bootstrap_view(runner)

    @app.delete("/api/sessions/{session_id}/bootstrap")
    def reset_bootstrap(session_id: str):
        """Back to the defaults: prompts.py and the repo's config."""
        from agent.bootstrap import settings

        runner = _runner(session_id)
        if runner.busy:
            raise _bad("running", session_id,
                       "Pause the session before resetting the design agent.", 409)
        settings.reset(runner.paths.session)
        return _bootstrap_view(runner)

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
        backends = _resolved_backends(runner, folder)
        view = editing.graph_view(folder, plan)
        # Onto the view nodes as well as in the table, so the drawing can show
        # the model without the front end learning how roles resolve.
        for node in view.get("nodes", []):
            node.update(backends.get(node.get("backend", ""), {}))

        return {
            **document,
            "editable": path == runner.paths.agent_dir,
            "view": view,
            "backends": backends,
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

    @app.put("/api/sessions/{session_id}/roles/{role}")
    def set_role(session_id: str, role: str, body: dict = Body(...)):
        """Choose the provider and model a role of the designed agent runs on
        -- every node naming that role -- for this session.

        Saved with the session (options.json, as the New dialog's Models
        section saves its choices) and used from the next time the run
        starts: a run builds its backends when it starts, so while one is in
        flight, Pause and Start to switch. An empty provider takes the
        session's choice back off the role.
        """
        from agent.backends import _check_access, _load
        from agent.config import BackendConfig

        runner = _runner(session_id)
        provider = str(body.get("provider") or "").strip()
        model = str(body.get("model") or "").strip()
        if provider and provider not in PROVIDERS:
            raise _bad("bad_provider", provider,
                       f"No provider {provider!r}. Known: {', '.join(sorted(PROVIDERS))}.", 422)

        try:
            folder = load_agent_folder(_agent_path(runner))
        except AgentFolderError as exc:
            raise _bad("unreadable_agent", session_id, str(exc), 422)
        needed = needed_for(folder)
        if role not in needed:
            raise _bad("no_role", role, f"No node of this agent runs on role {role!r}.", 404)
        if provider:
            try:
                _check_access(role, _load(provider),
                              BackendConfig(provider=provider, model=model or None),
                              needed[role])
            except Exception as exc:  # noqa: BLE001 -- BackendUnavailable, with its reason
                raise _bad("cannot_run_role", role, str(exc), 422)
        # The delegate: only when asked for, and it must be able to do the
        # role's job too -- antigravity cannot stand in for a read-only checker.
        wanted = body.get("delegate") or None
        if wanted is not None:
            d_provider = str(wanted.get("provider") or "").strip()
            d_model = str(wanted.get("model") or "").strip()
            if d_provider not in PROVIDERS:
                raise _bad("bad_provider", d_provider, "Choose a provider to delegate to.", 422)
            try:
                _check_access(role, _load(d_provider),
                              BackendConfig(provider=d_provider, model=d_model or None),
                              needed[role])
            except Exception as exc:  # noqa: BLE001
                raise _bad("cannot_delegate", role, str(exc), 422)

        options = storage.load_options(runner.paths)
        roles = [item for item in options.get("backend_role") or []
                 if str(item).partition("=")[0] != role]
        if provider:
            roles.append(f"{role}={provider}" + (f":{model}" if model else ""))
        if roles:
            options["backend_role"] = roles
        else:
            options.pop("backend_role", None)
        delegates = [item for item in options.get("delegate") or []
                     if str(item).partition("=")[0] != role]
        if wanted is not None:
            delegates.append(f"{role}={d_provider}" + (f":{d_model}" if d_model else ""))
        if delegates:
            options["delegate"] = delegates
        else:
            options.pop("delegate", None)
        # GPU: Codex only -- the other providers have no sandbox to switch off.
        role_options = dict(options.get("role_options") or {})
        if provider == "codex" and bool(body.get("gpu")):
            role_options[role] = {"gpu": True}
        else:
            role_options.pop(role, None)
        if role_options:
            options["role_options"] = role_options
        else:
            options.pop("role_options", None)
        storage.save_options(runner.paths, options)

        body_now = NewSession(repo=str(runner.paths.repo), **options)
        cfg = load_config(repo_path=runner.paths.repo,
                          cli=_Overrides(body_now, _DEFAULTS["cli_args"]))
        runner.cfg = cfg
        runner.runtime.cfg = cfg
        spec = backend_for(cfg, role)
        # The work phase builds its backends when the run starts: a change at
        # the design review (or any earlier stop) is used by that run; one
        # made while the agent is running waits for a Pause and a Start.
        when = ("The run in flight keeps its models until you Pause and Start."
                if runner.phase in ("running", "supervising") and runner.busy else
                "Used when the run starts.")
        gpu = (" with the GPUs (Codex's sandbox off for its write turns)"
               if spec.provider == "codex" and spec.options.get("gpu") else "")
        if role in (cfg.delegation or {}):
            d = cfg.delegation[role]
            gpu += (f"; if it runs out of credits, its turns go to {d.provider}"
                    + (f"/{d.model}" if d.model else ""))
        return {"saved": True, "role": role,
                "detail": f"{role} now runs on {spec.provider}"
                          + (f"/{spec.model}" if spec.model else "") + f"{gpu}. {when}",
                "backends": _resolved_backends(runner, folder)}

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
            context = editing.node_context(
                folder, name, state=state,
                artifacts_dir=str(runner.paths.artifacts),
                session_dir=str(runner.paths.session),
            )
            # Which MODEL this node's role resolves to. The role name alone
            # does not answer "what is about to read this prompt".
            context["resolved"] = _resolved_backends(runner, folder).get(
                context.get("backend", ""), {})
            return context
        except KeyError:
            raise _bad("no_node", name, f"No node named {name!r} in this agent.", 404)

    @app.get("/api/sessions/{session_id}/nodes/{name}/activity")
    def get_node_activity(session_id: str, name: str, limit: int = 20):
        """Every provider event for this node's turns, newest turn first.

        Separate from /context because it is the big one: a single executor
        turn can be hundreds of events with command output attached, and the
        Configure panel should not have to wait for that to render a form.
        """
        from agent import activity

        runner = _runner(session_id)
        turns = activity.turns(runner.paths.session, name, limit=limit)
        return {
            "node": name,
            "turns": [turn.as_dict() for turn in turns],
            "nodes_with_activity": activity.nodes_with_activity(runner.paths.session),
        }

    @app.get("/api/sessions/{session_id}/timeline")
    def get_timeline(session_id: str):
        """Every node visit in run order: node, model, conversation, route.

        The graph's state keeps only each node's latest route; this is the
        order -- which loop ran how many times, what a checker sent where,
        what the person approved -- for auditing a run after the fact.
        """
        from agent import activity

        return {"visits": activity.timeline(_runner(session_id).paths.session)}

    @app.get("/api/sessions/{session_id}/iterations")
    def get_iterations(session_id: str):
        """Every design-and-run iteration the supervisor reviewed, oldest first:
        its review and decision, the agent that ran, and the report it read."""
        from agent import supervisor

        session = _runner(session_id).paths.session
        root = session / supervisor.ITERATIONS
        out = []
        numbers = sorted(int(p.name) for p in root.iterdir()
                         if p.is_dir() and p.name.isdigit()) if root.is_dir() else []
        for n in numbers:
            folder = supervisor.iteration_dir(session, n)
            entry = {"n": n, "review": {}, "agent": "", "nodes": 0, "steps": 0,
                     "visits": 0, "report": ""}
            try:
                entry["review"] = json.loads((folder / "review.json").read_text())
            except (OSError, json.JSONDecodeError):
                pass
            try:
                graph = json.loads((folder / "agent" / "graph.json").read_text())
                entry["agent"] = graph.get("name", "")
                entry["nodes"] = len(graph.get("nodes", []))
            except (OSError, json.JSONDecodeError):
                pass
            try:
                entry["steps"] = len(json.loads((folder / "plan.json").read_text())
                                     .get("steps", []))
            except (OSError, json.JSONDecodeError):
                pass
            timeline = folder / "timeline.jsonl"
            if timeline.exists():
                entry["visits"] = sum(1 for line in timeline.read_text().splitlines()
                                      if line.strip())
            if (folder / "report.md").exists():
                entry["report"] = (folder / "report.md").read_text()
            out.append(entry)
        return {"iterations": out,
                "mode": _runner(session_id).cfg.supervisor,
                "max_iterations": _runner(session_id).cfg.max_iterations}

    @app.get("/api/sessions/{session_id}/files")
    def list_files(session_id: str):
        """The files in this session's own folder -- its agent, plan, reports
        and results. Nothing outside it: see webui/files.py."""
        from webui import files

        runner = _runner(session_id)
        return {"files": files.listing(runner.paths.session),
                "session": str(runner.paths.session)}

    @app.get("/api/sessions/{session_id}/files/content")
    def read_file(session_id: str, path: str = Query(...)):
        """One file's text.

        The path arrives from the browser, so it is the one untrusted input
        in this file -- `files.read` resolves it and refuses anything that
        lands outside the session folder. Taken as a QUERY parameter rather
        than a path segment on purpose: a path segment would need the slashes
        in "session/artifacts/report.md" escaped by every caller, and a caller
        that forgets gets a 404 that looks like a missing file.
        """
        from webui import files

        runner = _runner(session_id)
        try:
            return files.read(runner.paths.session, path)
        except PermissionError as exc:
            raise _bad("outside_session", path, str(exc), 403)
        except FileNotFoundError as exc:
            raise _bad("no_file", path, str(exc), 404)
        except OSError as exc:
            raise _bad("unreadable", path, str(exc), 422)

    @app.get("/api/sessions/{session_id}/activity")
    def get_current_activity(session_id: str):
        """The turn running right now, in full.

        This is what the "still working" line expands into. It exists as its
        own endpoint because the line does not say which node it belongs to,
        and making the browser track that would be asking it to hold state the
        server already has.
        """
        from agent import activity

        runner = _runner(session_id)
        turn = activity.in_flight(runner.paths.session)
        return {"running": turn is not None,
                "turn": turn.as_dict() if turn else None}

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
        root = _resolve_repo(repo)
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

    @app.get("/api/models")
    def models(provider: str | None = None, refresh: bool = False):
        """The models each provider offers, for the dropdowns that pick one.

        Asked of the providers themselves (see agent/backends/models.py), in
        parallel, and cached for a few minutes. A provider that cannot answer
        is still listed, with an empty list and the reason.
        """
        from agent.backends.models import list_all, list_models

        lists = ([list_models(provider, refresh=refresh)] if provider
                 else list_all(refresh=refresh))
        return {"providers": [listed.to_dict() for listed in lists]}

    @app.get("/api/health")
    def health():
        return {"ok": True, "open_sessions": sorted(_RUNNERS)}

    @app.get("/api/defaults")
    def defaults():
        """What this server was started with.

        The browser prefills its Repository field from `repo` rather than
        showing ".", so the value on screen is the actual path. A dialog that
        says "." is a dialog that does not tell you which project it is about
        to design an agent for.
        """
        repo = _DEFAULTS["repo"]
        return {
            "repo": str(repo),
            "sessions_dir": str(_sessions_root(repo)),
            "backend": getattr(_DEFAULTS["cli_args"], "backend", None),
            "model": getattr(_DEFAULTS["cli_args"], "model", None),
        }

    # ---- logins ----------------------------------------------------------
    #
    # A turn on a logged-out provider fails minutes in, with a message about
    # the model rather than about the login. These routes let the page say so
    # first, and fix it without leaving the page.

    @app.get("/api/auth")
    def auth_status(repo: str | None = None):
        """Every provider this repo would actually use, and whether it can run.

        Probed live rather than cached: the answer changes when somebody logs
        in elsewhere, and a cached "not logged in" is the kind of wrong that
        sends people to re-run a login that is already working.
        """
        cfg = load_config(repo_path=_resolve_repo(repo), cli=_DEFAULTS["cli_args"])
        in_use = {cfg.default.provider}
        in_use.update(backend_for(cfg, role).provider for role in cfg.roles)

        # Shown even when unused, so a provider can be signed in BEFORE being
        # configured -- which is the order people actually do it in.
        shown = sorted(in_use | set(auth_flows.FLOWS) | {"antigravity"})

        allowed, why = _login_allowed()
        return {
            "providers": [
                {**vars(status), "ok": status.ok, "in_use": status.provider in in_use}
                for status in backend_auth.status_all(shown)
            ],
            "can_login_here": allowed,
            "why_not": why,
        }

    @app.post("/api/auth/{provider}/login")
    def auth_login(provider: str, restart: bool = False):
        allowed, why = _login_allowed()
        if not allowed:
            raise _bad("login_refused", provider, why, status=403)
        try:
            return auth_flows.start(provider, restart=restart).snapshot()
        except auth_flows.LoginError as exc:
            raise _bad("login_unavailable", provider, str(exc))

    @app.get("/api/auth/{provider}/login")
    def auth_login_state(provider: str):
        session = auth_flows.get(provider)
        if session is None:
            raise _bad("no_login", provider, "No login has been started for "
                                             "this provider.", status=404)
        return session.snapshot()

    @app.post("/api/auth/{provider}/login/input")
    def auth_login_input(provider: str, body: dict = Body(...)):
        session = auth_flows.get(provider)
        if session is None:
            raise _bad("no_login", provider, "That login is no longer running. "
                                             "Start it again.", status=404)
        try:
            session.send(str(body.get("text") or ""))
        except auth_flows.LoginError as exc:
            raise _bad("login_closed", provider, str(exc))
        return session.snapshot()

    @app.post("/api/auth/{provider}/login/cancel")
    def auth_login_cancel(provider: str):
        session = auth_flows.get(provider)
        if session is not None:
            session.cancel()
        return {"ok": True}


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


def _sessions_root(repo: Path, chosen: str | None = None) -> Path:
    """The sessions root for `repo`: `chosen`, else what the server's flags and
    the repo's config say."""
    if not chosen:
        chosen = load_config(repo_path=repo, cli=_DEFAULTS["cli_args"]).sessions_root
    return storage.sessions_root(repo, chosen)


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


def _infrastructure_warning(repo: Path) -> str:
    """Refuse a repo that is, or is inside, an `.agent/` directory.

    An easy and entirely reasonable mistake, because the name suggests the
    session files go there -- but the relationship is the other way round:
    `.agent/` is CREATED INSIDE the repo. Point the repo at it and you get
    `.agent/.agent/sessions/...`, with the real project nowhere in sight.

    Worth refusing rather than warning. It would otherwise succeed, design an
    agent, and point that agent's read and write access at the checkpoint
    database and the folder describing the agent itself -- exactly what
    `work/compile.py::infrastructure_rules` spends three rules telling a write
    node never to touch.
    """
    parts = repo.parts
    if storage.DOT_AGENT not in parts:
        return ""

    index = parts.index(storage.DOT_AGENT)
    project = Path(*parts[:index]) if index else Path(repo.anchor)
    return (
        f"{storage.DOT_AGENT}/ is where this tool keeps its own files -- "
        f"sessions, checkpoints and designed agents -- and it is created "
        f"INSIDE the repository, not instead of it. Point this at the project "
        f"you want worked on and {storage.DOT_AGENT}/ appears there by itself."
        + (f" You probably meant {project}." if str(project) not in ("", "/") else "")
    )


def _why_not_a_repo(repo: Path) -> str:
    """Say what is wrong AND what exists nearby.

    "Not a directory" is true and useless. The two ways to get here are a path
    that does not exist and a path that is a file, and the fix differs -- and
    for the first, the sibling names are almost always the answer, because the
    mistake is nearly always a typo or a stale path.
    """
    if repo.exists():
        return f"{repo.name} exists but is a file, not a directory."

    parent = repo.parent
    if not parent.is_dir():
        return (f"Neither {repo} nor its parent {parent} exists. "
                f"Check the path from the top.")

    siblings = sorted(p.name for p in parent.iterdir() if p.is_dir())[:12]
    if not siblings:
        return f"{repo} does not exist, and {parent} has no subdirectories."
    return (f"{repo} does not exist. Directories in {parent}: "
            f"{', '.join(siblings)}")


def _resolve_repo(value: str | None) -> Path:
    """Turn the Repository field into a path.

    A relative path resolves against the repo this server was STARTED for, not
    against the server process's working directory -- and that difference was a
    real bug rather than a nicety. `python main.py web ../projectA` states which
    project you mean; the browser then defaulted its field to ".", which
    resolved to the cwd instead, so a session quietly put its `.agent/` beside
    the wrong project. Anchoring relative paths to the launched repo makes "."
    mean what the command line already said.

    Absolute paths and `~` are honoured as typed, so you can still point one
    server at several projects.
    """
    if not value or not value.strip():
        return _DEFAULTS["repo"]
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (_DEFAULTS["repo"] / path).resolve()


#: The NewSession fields that are choices about how to run, saved with the
#: session (storage.save_options).
_OPTION_FIELDS = ("backend", "model", "backend_role", "supervisor", "max_iterations",
                  "pre_build_agent", "research", "role_options", "delegate")


def _options_of(body: NewSession) -> dict:
    # `is not None` for the booleans: False ("research off") is a choice.
    return {name: getattr(body, name) for name in _OPTION_FIELDS
            if getattr(body, name) is not None and getattr(body, name) not in ([], "")}


def resume_interrupted(repo: Path) -> list[str]:
    """Start again every session that was running when the server stopped.

    Called by serve() at startup. A run is a server-side thing: closing the
    browser never stopped one, but restarting the server did, silently, and
    nothing brought it back. Each resumes from its checkpoint -- the node
    that was in flight runs again, everything before it is kept.
    """
    resumed = []
    for session_dir in storage.marked_running(repo):
        try:
            paths = storage.session_paths(repo, session_dir.name, session_dir=session_dir)
            task = _saved_task(paths)
            if not task:
                storage.unmark_running(paths)
                continue
            options = storage.load_options(paths)
            body = NewSession(repo=str(repo), session_dir=str(session_dir), **options)
            cfg = load_config(repo_path=repo, cli=_Overrides(body, _DEFAULTS["cli_args"]))
            runner = _open_runner(paths.session.name, cfg, paths)
            if not runner.busy:
                runner.start(task, pre_build_agent=options.get("pre_build_agent"))
            resumed.append(paths.session.name)
        except (Exception, SystemExit) as exc:  # noqa: BLE001 -- one bad session must not stop the server
            print(f"Could not resume {session_dir}: {exc}")
    return resumed


def forget_interrupted(repo: Path) -> list[str]:
    """Drop every resume mark without resuming anything (--no-resume)."""
    names = []
    for session_dir in storage.marked_running(repo):
        (Path(repo).resolve() / storage.DOT_AGENT / "running"
         / f"{session_dir.name}.json").unlink(missing_ok=True)
        names.append(session_dir.name)
    return names


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
        commands=runner.pending_commands if runner.pending else [],
        last_seq=runner.bus.last_seq,
        # From the checkpoint, not the bus: a live run keeps the chat current
        # through `state` events, but a session opened in a FRESH server has
        # an empty bus and all of its history still on disk.
        transcript=storage.read_transcript(paths),
        error=runner.error,
        problems=problems,
    )


def _bootstrap_view(runner: SessionRunner) -> dict:
    from agent.bootstrap import settings

    try:
        view = settings.describe(runner.cfg, runner.paths.session)
    except settings.SettingsError as exc:
        raise _bad("bad_bootstrap", exc.where, exc.message, 422)
    transcript = storage.read_transcript(runner.paths)
    return {
        **view,
        # Editable whenever nothing is running -- including after the
        # conversation has begun, since an edit then applies from the next
        # turn after a resume.
        "editable": not runner.busy,
        "started": bool(transcript) or runner.busy or runner.paths.has_agent(),
        "busy": runner.busy,
    }


def _resolved_backends(runner, folder) -> dict:
    """role name -> the provider and model it actually resolves to.

    A node declares a ROLE -- "coder", "checker" -- and which model that is
    depends on config resolution, three layers of it. So the graph could tell
    you a node runs on "coder" and nothing on the page could tell you whether
    that was gpt-5.6-sol or gemini-3.8-flash-medium, which is the difference
    between a careful verifier and a cheap one, and the whole reason roles are
    separate from models.

    Resolved once per request and keyed by role, not by node: two nodes on the
    same role share an answer, and this keeps the front end from having to
    know anything about config layering.
    """
    from agent.agentfolder.schema import AgentNodeConfig

    # The SESSION's settings -- its New-dialog models and the changes made
    # with "Runs on" -- not the repository's alone, which is what this read
    # before, so a session started on other models showed the wrong ones.
    cfg = runner.cfg
    users: dict[str, list[str]] = {}
    for name, config in folder.nodes.items():
        if isinstance(config, AgentNodeConfig) and config.backend:
            users.setdefault(config.backend, []).append(name)
    chosen = _role_overrides(runner.paths)

    resolved = {}
    for role in sorted(users):
        spec = backend_for(cfg, role)
        resolved[role] = {
            "provider": spec.provider,
            "model": spec.model,
            # False means "nobody named this role, it fell through to the
            # default" -- worth showing, because that is how an experiment
            # ends up on a model nobody chose.
            "configured": role in cfg.roles,
            "session_choice": role in chosen,
            "nodes": users[role],
            # Codex with its sandbox off for write turns, so they see the GPUs.
            "gpu": spec.provider == "codex" and bool(spec.options.get("gpu")),
            # Where its turns go if its provider runs out of credits -- None
            # unless someone chose, for this role.
            "delegate": ({"provider": cfg.delegation[role].provider,
                          "model": cfg.delegation[role].model or ""}
                         if role in (cfg.delegation or {}) else None),
        }
    return resolved


def _role_overrides(paths) -> dict[str, str]:
    """role -> "provider[:model]", as this session's options.json holds them."""
    out = {}
    for item in storage.load_options(paths).get("backend_role") or []:
        role, _, spec = str(item).partition("=")
        if role and spec:
            out[role] = spec
    return out


def _login_allowed() -> tuple[bool, str]:
    """Whether this server may sign the machine in to anything.

    Loopback only, and this is not paranoia about a local tool. The web UI has
    no token and no password -- webui/README.md says so plainly -- so on
    `--host 0.0.0.0` anyone who can reach the port could log this machine into
    THEIR account, and every turn after that would run as them, on their bill,
    with their credentials in ~/.codex. That is a different and worse thing
    than the arbitrary-code-execution the README already warns about, because
    it leaves no trace on the page.

    Reading the status is still allowed from anywhere: it says whether a login
    exists, never what it is.
    """
    host = str(_DEFAULTS.get("host") or "")
    if host in ("127.0.0.1", "localhost", "::1", ""):
        return True, ""
    return False, (
        f"This server is bound to {host}, so it is reachable from other "
        f"machines -- and it has no password. Signing in from here would let "
        f"anyone who can reach the port log this machine into their own "
        f"account. Run the login in a terminal on the server instead, or "
        f"restart the UI on 127.0.0.1 and reach it over an SSH tunnel."
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

    app = create_app(repo, cli_args, host=host)
    print(f"\nAgent designer UI:  http://{host}:{port}\n")
    if getattr(cli_args, "no_resume", False):
        # Not resumed, and forgotten: otherwise they would come back at the
        # next ordinary start, which is not what "no" meant.
        for name in forget_interrupted(Path(repo).resolve()):
            print(f"Not resuming {name} (--no-resume). Open it and press Start "
                  f"to carry on.", flush=True)
    else:
        for name in resume_interrupted(Path(repo).resolve()):
            print(f"Resumed {name}: it was running when the server last stopped.", flush=True)
    uvicorn.run(
        app, host=host, port=port, log_level="warning",
        # Without a bound, Ctrl-C waits for every open connection -- and an SSE
        # stream is open by definition. Five seconds is long enough for the
        # streams to notice and short enough that Ctrl-C feels like Ctrl-C.
        timeout_graceful_shutdown=5,
    )
