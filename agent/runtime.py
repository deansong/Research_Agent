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
differs between the two front ends: the CLI derives a session from --task and
refuses when the task changed; the web UI has a session picker. So
`_resolve_session` stays in cli.py, and the runtime is handed a `SessionPaths`
that someone else already resolved.

The rule this file follows: **it decides how to BUILD things, never when to do
them or what to say about it.**
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from langgraph.checkpoint.sqlite import SqliteSaver

from agent import storage
from agent.agentfolder.load import AgentFolder, AgentFolderError, load_agent_folder
from agent.agentfolder.validate import Problem, validate_folder
from agent.backends import build_backends
from agent.backends.base import Access, BackendError
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

        # SqliteSaver.from_conn_string does NOT create parent directories, but
        # session_paths() has already made them -- see storage.py.
        self.checkpointer = stack.enter_context(
            SqliteSaver.from_conn_string(str(paths.checkpoint))
        )
        self.checkpointer.setup()

    # ---- backends --------------------------------------------------------

    def codex_client(self, needed):
        """Open the Codex client on first use, and only if something needs it.

        Lazy for two reasons. The work graph's roles are not known until phase 1
        has designed the agent, so we cannot decide up front whether any of them
        uses Codex. And being lazy is what lets `--backend fake` run with no
        login and no network at all.
        """
        if not any(backend_for(self.cfg, role).provider == "codex" for role in needed):
            return self._codex
        if self._codex is None:
            from agent.backends.codex import open_client

            self._codex = self._stack.enter_context(open_client())
        return self._codex

    def backends(self, needed) -> dict[str, Any]:
        """Build one backend per role. Raises BackendError, which is a real
        error condition rather than a presentation choice, so it stays an
        exception."""
        return build_backends(self.cfg, needed, codex_client=self.codex_client(needed))

    # ---- phase 1: designing an agent -------------------------------------

    def bootstrap_session(self, request: str) -> GraphSession:
        """A GraphSession for the hand-written design graph.

        The thread id uses paths.session.name rather than cfg.session: the
        latter is None whenever the name was derived from the task or came from
        --session-dir, which would make every such run share the literal thread
        id "None:bootstrap".
        """
        graph = build_bootstrap_graph(
            self.backends(BOOTSTRAP_ACCESS),
            self.paths,
            self.checkpointer,
            max_attempts=self.cfg.max_design_attempts,
        )
        return bootstrap_session(
            graph=graph,
            thread_id=f"{self.paths.session.name}:bootstrap",
            recursion_limit=self.cfg.recursion_limit,
            repo_path=str(self.paths.repo),
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
        if self.paths.has_agent() and self.paths.is_approved():
            return self.paths.agent_dir
        # A designed but unapproved agent deliberately returns None, so the
        # caller re-enters the design phase -- where the bootstrap checkpoint
        # is still parked at the review question. Coming back to a session
        # mid-review puts you back at the same gate.
        return None

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
        )

        # Every phase and every task gets its OWN thread id. Measured: two
        # graphs sharing a thread id do not raise -- their channels silently
        # merge, and one schema's keys turn up in the other's state.
        suffix = f":{run_index}" if run_index > 1 else ""

        return work_session(
            folder=folder,
            graph=graph,
            thread_id=f"{self.paths.session.name}:work{suffix}",
            recursion_limit=self.cfg.work_recursion_limit,
            initial_state=initial_work_state(
                repo_path=str(self.paths.repo),
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
