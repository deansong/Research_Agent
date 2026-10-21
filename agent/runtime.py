"""
WHAT:  Everything needed to RUN this agent, with nothing that talks to a human.
WHY:   There are two front ends now -- the terminal REPL and the web UI -- and
       the moment there are two, "how do we build the objects" has to stop
       living inside "how do we talk to the user".
CONCEPT: A composition root, separated from its presentation.

--------------------------------------------------------------------------
WHAT MOVED HERE, AND WHY EACH PIECE HAD TO
--------------------------------------------------------------------------
All of this used to be inside `cli.py::main()`. Three things about it were
terminal-shaped, and only the third is obvious:

1. It PRINTED. Forty-odd print() calls, interleaved with the logic, so there
   was no way to get at "which agent did we resolve" without also getting the
   sentence announcing it.

2. It raised SystemExit, carrying multi-line human-readable remedies. Lovely in
   a terminal. In a server, SystemExit is a 500 and the remedy is in a log file
   nobody reads. Here, problems come back as VALUES -- see `FolderLoad` -- and
   whoever is presenting decides how to say it.

3. Its `ExitStack` was scoped to a single function call. The SqliteSaver and
   the Codex client lived exactly as long as `main()` did. A web server needs
   them to outlive one HTTP request, so the stack has to belong to an object
   with a lifetime, not to a function.

--------------------------------------------------------------------------
WHAT DID *NOT* MOVE
--------------------------------------------------------------------------
Deciding WHICH task, WHICH session and WHEN to redesign is policy, and it
differs between the two front ends: the CLI starts a new session per task and
refuses when a named one's task changed; the web UI has a session picker. So
`_resolve_session` stays in cli.py, and the runtime is handed a `SessionPaths`
that someone else already resolved.

The rule this file follows: **it decides how to BUILD things, never when to do
them or what to say about it.**
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator

from langgraph.checkpoint.sqlite import SqliteSaver

from agent import storage
from agent.agentfolder.load import AgentFolder, AgentFolderError, load_agent_folder
from agent.agentfolder.validate import Problem, validate_folder
from agent.backends import build_backends
from agent.backends.base import Access, BackendError
from agent.backends.delegate import DelegatingBackend
from agent.bootstrap.graph import build_bootstrap_graph
from agent.bootstrap.session import bootstrap_session
from agent.config import AgentConfig, backend_for
from agent.session import GraphSession
from agent.work.compile import backends_needed, compile_agent
from agent.work.session import work_session
from agent.work.state import initial_work_state

#: The three roles the hand-written bootstrap graph runs, and the access each
#: needs. Unlike a generated agent -- whose roles come from its nodes.json --
#: these are fixed, because the graph that uses them is fixed.
BOOTSTRAP_ACCESS: dict[str, Access] = {
    "discussor": Access.READ_ONLY,
    "planner": Access.READ_ONLY,
    "designer": Access.READ_ONLY,
}

#: The research agent, when research is on. WRITE because antigravity, its
#: default provider, has no read-only level, and its paper lookups are
#: commands it runs. See agent/bootstrap/nodes/researcher.py.
RESEARCH_ACCESS: dict[str, Access] = {"researcher": Access.WRITE}


@dataclass(frozen=True)
class FolderLoad:
    """The result of trying to load an agent folder, as data rather than exits.

    `folder` is None when the folder could not even be READ (missing file, bad
    JSON, wrong shape) -- `error` then says why. When `folder` is present it may
    still carry `problems`; that is a different and much more interesting state,
    because a folder can be readable and structurally wrong at the same time.

    The blocking/warning split is what lets an editor save a half-wired graph
    while refusing to RUN it -- you cannot rewire a graph without passing
    through invalid states, so refusing to save them makes editing impossible.
    """

    path: Path
    folder: AgentFolder | None = None
    error: str = ""
    problems: tuple[Problem, ...] = ()

    @property
    def blocking(self) -> list[Problem]:
        """Problems that must be fixed before this agent can run."""
        return [p for p in self.problems if not p.warning]

    @property
    def runnable(self) -> bool:
        return self.folder is not None and not self.blocking


class Runtime:
    """Live objects for one session: the checkpointer, and a lazy Codex client.

    Created by `open_runtime()`. Everything it hands out -- GraphSessions,
    compiled graphs, backends -- is built on demand, because the work phase's
    roles are not known until the design phase has finished inventing them.
    """

    def __init__(self, cfg: AgentConfig, paths: storage.SessionPaths, stack):
        self.cfg = cfg
        self.paths = paths
        self._stack = stack
        self._codex = None
        self._built: dict[str, Any] = {}

        # SqliteSaver.from_conn_string does NOT create parent directories, but
        # session_paths() has already made them -- see storage.py.
        self.checkpointer = stack.enter_context(
            SqliteSaver.from_conn_string(str(paths.checkpoint))
        )
        self.checkpointer.setup()

    # ---- backends --------------------------------------------------------

    def codex_client(self, needed, cfg: AgentConfig | None = None):
        """Open the Codex client on first use, and only if something needs it.

        Lazy for two reasons. The work graph's roles are not known until phase 1
        has designed the agent, so we cannot decide up front whether any of them
        uses Codex. And being lazy is what lets `--backend fake` run with no
        login and no network at all.
        """
        cfg = cfg or self.cfg
        if not any(backend_for(cfg, role).provider == "codex" for role in needed):
            return self._codex
        if self._codex is None:
            from agent.backends.codex import open_client

            self._codex = self._stack.enter_context(open_client())
        return self._codex

    def backends(self, needed, cfg: AgentConfig | None = None) -> dict[str, Any]:
        """Build one backend per role. Raises BackendError, which is a real
        error condition rather than a presentation choice, so it stays an
        exception.

        Every backend built through here is remembered, so a caller can reach
        them all later -- which is what `arm_cancel` needs. build_backends
        caches identically-configured roles onto one instance, so the same
        object legitimately appears under several role names.
        """
        cfg = cfg or self.cfg
        built = build_backends(cfg, needed, codex_client=self.codex_client(needed, cfg))
        built = dict(built)
        for role, spec in (cfg.delegation or {}).items():
            if role not in needed:
                continue
            # Its OWN primary instance: build_backends shares one instance
            # between roles with the same settings, and a delegating role's
            # primary is told not to wait for credits -- which must not reach
            # a role that does not delegate.
            primary = build_backends(cfg, {role: needed[role]},
                                     codex_client=self.codex_client(needed, cfg))[role]
            stand_in = replace(cfg, roles={**cfg.roles, role: spec})
            delegate = build_backends(stand_in, {role: needed[role]},
                                      codex_client=self.codex_client({role: needed[role]},
                                                                     stand_in))[role]
            built[role] = DelegatingBackend(primary, delegate, role)
        # A session with its own workspace runs turns THERE, and a sandbox
        # that allows writing only in the working directory would then refuse
        # the run's results folder, which sits beside it. So name it.
        writable = [str(self.paths.artifacts)] if self.paths.own_workspace else []
        for backend in built.values():
            backend.writable_roots = writable
        self._built.update(built)
        return built

    def arm_cancel(self, event, pause=None) -> None:
        """Give every backend a way to be told to stop mid-turn.

        Only the provider can stop a turn that is already running, so the
        signal has to reach that far. Setting an attribute rather than passing
        a constructor argument on purpose: backends are cached by their config,
        so one instance is shared across roles, and cancellation belongs to the
        RUN rather than to the configuration.
        """
        for backend in self._built.values():
            # A backend that does not know about cancellation just carries an
            # attribute nothing reads -- harmless, and better than each caller
            # having to know which providers support it.
            backend.cancel = event
            # Pause, too, but only for retry waits: see wait_or_cancel.
            backend.pause = pause

    # ---- phase 1: designing an agent -------------------------------------

    def bootstrap_session(self, request: str) -> GraphSession:
        """A GraphSession for the hand-written design graph.

        The thread id uses paths.session.name rather than cfg.session: the
        latter is None for every new session and whenever the folder came from
        --session-dir, which would make every such run share the literal thread
        id "None:bootstrap".
        """
        # This session's own changes to the design agent, if it has any --
        # read at every start, so an edit made while paused applies on resume.
        from agent.bootstrap import settings

        overrides = settings.load(self.paths.session)
        cfg = settings.effective_config(self.cfg, overrides)
        backends = dict(self.backends(BOOTSTRAP_ACCESS, cfg))
        if cfg.research:
            # A help, not a requirement: a research agent that cannot start
            # (agy missing or signed out) must not stop the discussion.
            try:
                backends.update(self.backends(RESEARCH_ACCESS, cfg))
            except BackendError as exc:
                print(f"Note: research is on, but the research agent cannot start, "
                      f"so the discussor goes without it:\n  {str(exc).splitlines()[0]}")
        graph = build_bootstrap_graph(
            backends,
            self.paths,
            self.checkpointer,
            max_attempts=cfg.max_design_attempts,
            prompts=settings.effective_prompts(overrides),
            research=cfg.research,
        )
        return bootstrap_session(
            graph=graph,
            thread_id=f"{self.paths.session.name}:bootstrap",
            recursion_limit=self.cfg.recursion_limit,
            repo_path=str(self.paths.workspace),
            session_dir=str(self.paths.session),
            request=request,
        )

    def save_brief(self, values: dict) -> str:
        """Persist the brief the designer wrote for the new agent, and return it.

        Called after the design phase succeeds. Returns "" when there is none,
        so the caller can decide whether that is worth mentioning.
        """
        brief = (values.get("design") or {}).get("task_brief", "").strip()
        if brief:
            self.paths.brief.write_text(brief + "\n")
        return brief

    # ---- resolving and checking a folder ---------------------------------

    def resolve_folder(self, pre_build_agent: str | None) -> Path | None:
        """Which folder to run, or None meaning "design one first".

        Order: an explicitly requested agent, then this session's own. The
        middle case is what makes a session resumable -- once the validator has
        renamed agent.tmp/ to agent/, that folder is the durable record of what
        this session runs.
        """
        if pre_build_agent:
            # An agent you named explicitly needs no review: you chose it, and
            # it is not this session's own design.
            return storage.resolve_agent(pre_build_agent, self.paths)
        if self.paths.has_agent() and not self.paths.is_approved():
            self._grandfather_pre_review_approval()
        if self.paths.has_agent() and self.paths.is_approved():
            return self.paths.agent_dir
        # A designed but unapproved agent deliberately returns None, so the
        # caller re-enters the design phase -- where the bootstrap checkpoint
        # is still parked at the review question. Coming back to a session
        # mid-review puts you back at the same gate.
        return None

    def _grandfather_pre_review_approval(self) -> None:
        """Treat an agent from before the review gate as already approved.

        Adding the gate would otherwise break every session that already had a
        working agent: no marker on disk means "not approved", so opening one
        would send it back through the design phase -- and since its bootstrap
        graph had already finished, that means starting the discussion again
        from nothing. A change that improves the next run must not do that to
        the last one.

        The signal is exact rather than a guess about dates. Before the gate
        existed, the validator set `outcome = "ready"` the moment a design
        passed, and that is what ended the design phase. So a bootstrap thread
        whose state already says "ready" was approved under the old rules. A
        session genuinely parked at the new gate has no outcome yet, so it
        keeps its gate.
        """
        try:
            snapshot = self.checkpointer.get_tuple(
                {"configurable": {"thread_id": f"{self.paths.session.name}:bootstrap"}}
            )
        except Exception:  # noqa: BLE001 -- a missing or odd checkpoint is not fatal
            return
        if snapshot is None:
            return
        values = snapshot.checkpoint.get("channel_values") or {}
        if values.get("outcome") == "ready":
            self.paths.approve(
                "approved before the design-review gate existed; "
                "grandfathered on first open\n"
            )

    def fallback_folder(self) -> Path:
        """The agent to use when the design phase finished without writing one
        (the `/use` command at design_failed)."""
        return storage.resolve_agent(self.cfg.default_agent, self.paths)

    def load_folder(self, path: Path) -> FolderLoad:
        """Read and check a folder, reporting rather than exiting.

        Two stages, deliberately separate: `load_agent_folder` checks SHAPE (is
        this valid JSON matching the schema), `validate_folder` checks SENSE (is
        every node reachable, does every enum case have a target). A folder can
        pass the first and fail the second, and the second's failures are the
        interesting ones.
        """
        try:
            folder = load_agent_folder(path)
        except AgentFolderError as exc:
            return FolderLoad(path=path, error=str(exc))
        return FolderLoad(path=path, folder=folder,
                          problems=tuple(validate_folder(folder)))

    # ---- phase 2: running the designed agent -----------------------------

    def work_session(
        self,
        folder: AgentFolder,
        *,
        backends: dict[str, Any],
        task_brief: str,
        run_index: int = 1,
        threads: dict[str, str] | None = None,
        iteration: int = 1,
    ) -> GraphSession:
        """A GraphSession for one task against one agent.

        `run_index` exists because a /new-style command ends a run with
        outcome="new_task" and the next task gets a FRESH thread rather than a
        reset state -- resetting in place would need a "write arbitrary state"
        primitive in the folder format, which is exactly what the two-template
        restriction exists to prevent.
        """
        from agent.agentfolder.commands import build_registry

        graph = compile_agent(
            folder,
            backends=backends,
            checkpointer=self.checkpointer,
            registry=build_registry(folder),
            artifacts_dir=str(self.paths.artifacts),
            session_dir=str(self.paths.session),
            isolation=storage.isolation_rules(self.paths),
        )

        # Every phase and every task gets its OWN thread id. Measured: two
        # graphs sharing a thread id do not raise -- their channels silently
        # merge, and one schema's keys turn up in the other's state.
        suffix = f":{run_index}" if run_index > 1 else ""
        # A supervisor's later iteration is a new agent: a new thread too.
        if iteration > 1:
            suffix = f":i{iteration}" + suffix

        return work_session(
            folder=folder,
            graph=graph,
            thread_id=f"{self.paths.session.name}:work{suffix}",
            recursion_limit=self.cfg.work_recursion_limit,
            initial_state=initial_work_state(
                repo_path=str(self.paths.workspace),
                task_brief=task_brief,
                agent_dir=str(folder.path),
                artifacts_dir=str(self.paths.artifacts),
                plan=self.load_plan(),
                threads=threads or {},
            ),
        )

    def adopt_new_task(self, task_brief: str) -> None:
        """Point this session at a different task, keeping the same agent.

        This DELIBERATELY does the thing `cli._warn_if_task_changed` refuses.
        The difference is consent: there, a stale session would silently hijack
        a new task; here you typed a command asking for exactly this. Both files
        are updated so the session's recorded identity matches what it now does.
        """
        self.paths.request.write_text(task_brief)
        self.paths.brief.write_text(task_brief)

    # ---- the supervisor --------------------------------------------------

    def supervisor_session(self, *, iteration: int, values: dict,
                           task_brief: str, agent_dir: Path) -> GraphSession:
        """A GraphSession that reviews the run that just ended and decides
        what happens next. See agent/supervisor.py."""
        from agent import supervisor

        backend = self.backends({supervisor.ROLE: Access.READ_ONLY})[supervisor.ROLE]
        report = supervisor.run_report(
            session_dir=self.paths.session, artifacts_dir=self.paths.artifacts,
            plan=self.load_plan(), brief=task_brief, values=values, iteration=iteration)
        return supervisor.supervisor_session(
            graph=supervisor.build_supervisor_graph(backend, self.checkpointer),
            thread_id=f"{self.paths.session.name}:supervisor:{iteration}",
            initial_state={
                "repo_path": str(self.paths.workspace), "session_dir": str(self.paths.session),
                "agent_dir": str(agent_dir), "iteration": iteration,
                "max_iterations": self.cfg.max_iterations, "mode": self.cfg.supervisor,
                "report": report,
            },
        )

    def reenter_design(self, *, decision: str, feedback: str, auto: bool,
                       request: str) -> GraphSession:
        """The design phase again, from the planner (replan) or the designer
        (redesign), carrying the supervisor's feedback.

        The design graph's thread ended at "ready". update_state(as_node=
        "human") writes the next step into it as if the person had just typed
        the command, so the graph carries on from there -- same thread, same
        conversation history, same checkpoint. With `auto`, the plan and
        design approvals are answered for you (bootstrap/nodes/human.py).
        """
        session = self.bootstrap_session(request)
        graph, config = session.graph, session.config
        values = graph.get_state(config).values or {}
        design = dict(values.get("design") or {})
        human = dict(values.get("human") or {})
        design.update(problems="", attempt=0, review_feedback=feedback)
        if decision == "replan":
            design["plan_feedback"] = feedback
        human.update(question="", context="", purpose="",
                     return_to="planner" if decision == "replan" else "designer",
                     auto_approve=auto)
        graph.update_state(config, {"design": design, "human": human, "outcome": ""},
                           as_node="human")
        return session

    # ---- the plan --------------------------------------------------------

    def load_plan(self) -> dict:
        """Read plan.json, if this session has one.

        Absent under --pre-build-agent, where no planning happened: {my_steps}
        then renders empty and the node falls back to {task_brief}, which is the
        same graceful degradation every placeholder has.

        Invalid JSON is also survivable -- a half-saved edit should not make a
        session unusable -- so it returns ({}, reason) rather than raising.
        """
        plan, _ = self.load_plan_reporting()
        return plan

    def load_plan_reporting(self) -> tuple[dict, str]:
        """load_plan(), plus a sentence explaining an empty result."""
        if not self.paths.plan.exists():
            return {}, ""
        try:
            return json.loads(self.paths.plan.read_text()), ""
        except json.JSONDecodeError as exc:
            return {}, f"{self.paths.plan} is not valid JSON ({exc}); ignoring it."


@contextlib.contextmanager
def open_runtime(cfg: AgentConfig, paths: storage.SessionPaths) -> Iterator[Runtime]:
    """Open the checkpointer (and, on demand, Codex) for one session.

    A context manager rather than a constructor because the things it owns need
    closing, and because an ExitStack is exactly the right tool for "several
    resources, some of which may never be opened".
    """
    with contextlib.ExitStack() as stack:
        yield Runtime(cfg, paths, stack)


def needed_for(folder: AgentFolder) -> dict[str, Access]:
    """Re-exported so a caller does not have to import from work.compile to ask
    a question about a folder."""
    return backends_needed(folder)


__all__ = [
    "BOOTSTRAP_ACCESS",
    "BackendError",
    "FolderLoad",
    "Runtime",
    "needed_for",
    "open_runtime",
]
