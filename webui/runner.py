"""
WHAT:  Drives a graph for the web, the way agent/terminal.py::drive() drives it
       for a terminal.
WHY:   drive() blocks on input(). HTTP cannot block for four minutes waiting for
       someone to read a question and type an answer.
CONCEPT: The same interrupt/resume cycle, with the human turned inside out.

--------------------------------------------------------------------------
READ drive() FIRST -- THEY ARE THE SAME LOOP
--------------------------------------------------------------------------
agent/terminal.py::drive() is:

    result = invoke(initial_state)
    while there is an interrupt:
        answer = input()                     <-- blocks the whole program
        result = invoke(Command(resume=answer))

_pump() below is:

    result = invoke(initial_state)
    while there is an interrupt:
        emit a "question" event
        answer = self._answers.get()         <-- blocks ONLY this thread
        result = invoke(Command(resume=answer))

That single substitution is the whole design. It works because of a decision
made long before this file existed: `agent/commands.py:39-51` says the value
passed to Command(resume=...) must be a bare string, and that the node re-parses
it, so "the graph must stay safe when driven by something that is not our
terminal (a test, a future web UI)". The browser POSTs the same text you would
type. `/plan` and `/approve` need no special handling at all.

--------------------------------------------------------------------------
ONE RUN PER SESSION
--------------------------------------------------------------------------
A session owns one SQLite checkpoint and one LangGraph thread id. Two runs
against those at once is data corruption, not a race we can tune away -- and
two browser tabs on one URL is the normal case, not the exotic one. So a
SessionRunner holds at most one worker thread, and `answer()` refuses when the
worker is busy rather than trusting the UI to be well behaved.
"""

from __future__ import annotations

import queue
import threading
import traceback
from dataclasses import dataclass
from typing import Any

from langgraph.types import Command

from agent import commands, storage
from agent.config import AgentConfig
from agent.runtime import Runtime, open_runtime
from agent.terminal import _interrupt_payload
from webui import capture
from webui.events import EventBus

#: Returned by the pump when the human abandoned the run.
_STOP = object()


@dataclass
class Pending:
    """A question the graph is parked on, waiting for a human."""

    purpose: str
    question: str
    context: str = ""

    @classmethod
    def from_payload(cls, payload: dict) -> "Pending":
        return cls(
            purpose=payload.get("purpose", ""),
            question=payload.get("question", ""),
            context=payload.get("context", ""),
        )

    def as_dict(self) -> dict:
        return {"purpose": self.purpose, "question": self.question,
                "context": self.context}


class SessionRunner:
    """One session's live state: its runtime, its worker, its events.

    Held by the server for as long as the session is open. Deliberately NOT
    per-request: the checkpointer and the Codex client take real time to open,
    and the worker thread has to survive between the question and the answer.
    """

    def __init__(self, session_id: str, cfg: AgentConfig, paths: storage.SessionPaths):
        self.id = session_id
        self.cfg = cfg
        self.paths = paths
        self.bus = EventBus()

        self.phase = "idle"        # idle | designing | running | finished
        self.pending: Pending | None = None
        self.folder_path = None
        self.last_values: dict[str, Any] = {}
        self.error: str = ""

        self._answers: queue.Queue = queue.Queue(maxsize=1)
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

        # The runtime owns the checkpointer, so it must outlive every request.
        # ExitStack normally closes on leaving a `with`; here we drive the
        # context manager by hand and close it in `shutdown()`.
        self._runtime_cm = open_runtime(cfg, paths)
        self.runtime: Runtime = self._runtime_cm.__enter__()

    # ---- lifecycle -------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    @property
    def waiting(self) -> bool:
        """Parked on a question, with a worker alive to receive the answer."""
        return self.busy and self.pending is not None

    def shutdown(self) -> None:
        """Close the checkpointer and the provider client."""
        self.bus.close()
        if self._worker is not None and self._worker.is_alive():
            # Unblock a worker parked on the answer queue so its thread can end.
            with self._lock:
                self.pending = None
            try:
                self._answers.put_nowait(_STOP)
            except queue.Full:
                pass
            self._worker.join(timeout=5.0)
        try:
            self._runtime_cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 -- shutdown must not raise
            pass

    # ---- the two things a client can do ----------------------------------

    def start(self, task_brief: str, *, pre_build_agent: str | None = None) -> None:
        """Begin (or resume) this session on a worker thread."""
        if self.busy:
            raise RuntimeError("This session is already running.")
        self.bus.reopen()
        self.error = ""
        self._drain_answers()
        self._worker = threading.Thread(
            target=self._run,
            args=(task_brief, pre_build_agent),
            name=f"session-{self.id}",
            daemon=True,
        )
        self._worker.start()

    def answer(self, text: str) -> None:
        """Hand a human's reply to the waiting worker.

        Refuses unless a question is actually outstanding. Two tabs, or a
        double-clicked Send button, otherwise queue a second answer that the
        graph consumes at the NEXT question -- answering a question nobody saw
        being asked, which is a genuinely confusing bug to be on the wrong end of.
        """
        with self._lock:
            if not self.waiting:
                raise RuntimeError(
                    "Nothing is waiting for an answer right now."
                    if self.busy else
                    "This session is not running. Start it first."
                )
            self.pending = None
        self._answers.put(text)

    # ---- the worker ------------------------------------------------------

    def _run(self, task_brief: str, pre_build_agent: str | None) -> None:
        """The whole of one session, on one thread.

        `capture.routed_to` is what makes a node's print() land in the browser;
        see webui/capture.py for why it is done this way.
        """
        with capture.routed_to(lambda line: self.bus.emit("log", text=line)):
            try:
                self._phases(task_brief, pre_build_agent)
            except BaseException as exc:  # noqa: BLE001
                # A worker thread that dies silently is the worst failure mode
                # here: the browser sits on a spinner forever with no clue why.
                self.error = f"{type(exc).__name__}: {exc}"
                self.bus.emit(
                    "error",
                    code=type(exc).__name__,
                    where=f"session {self.id}",
                    message=str(exc),
                    detail=traceback.format_exc(limit=8),
                )
            finally:
                with self._lock:
                    self.pending = None
                self.phase = "finished"
                self.bus.emit("done", error=self.error)

    def _phases(self, task_brief: str, pre_build_agent: str | None) -> None:
        """Design an agent if there isn't one, then run it.

        The same two phases cli.py runs, minus the commentary.
        """
        runtime = self.runtime

        folder_path = runtime.resolve_folder(pre_build_agent)

        if folder_path is None:
            if not task_brief:
                raise RuntimeError("No task supplied.")
            self._set_phase("designing")
            values = self._pump(runtime.bootstrap_session(task_brief))
            if values is _STOP:
                return
            if values.get("outcome") != "ready":
                self.bus.emit("status", phase="idle",
                              detail="Stopped before an agent was ready.")
                return
            brief = runtime.save_brief(values)
            if brief:
                task_brief = brief
            folder_path = runtime.resolve_folder(None) or runtime.fallback_folder()

        loaded = runtime.load_folder(folder_path)
        if loaded.folder is None:
            raise RuntimeError(f"Could not read the agent at {folder_path}: {loaded.error}")

        self.folder_path = loaded.path
        if loaded.problems:
            for problem in loaded.problems:
                self.bus.emit("error", code=problem.code, where=problem.where,
                              message=problem.message, warning=problem.warning)
        if loaded.blocking:
            raise RuntimeError("That agent cannot run until its problems are fixed.")

        from agent.runtime import needed_for

        backends = runtime.backends(needed_for(loaded.folder))
        self._set_phase("running", agent=loaded.folder.graph.name)
        self._work_loop(loaded.folder, backends, task_brief)

    def _work_loop(self, folder, backends, task_brief: str) -> None:
        """Run the agent, once per task -- the /new cycle from cli.py."""
        threads: dict[str, str] = {}
        run_index = 1

        while True:
            session = self.runtime.work_session(
                folder, backends=backends, task_brief=task_brief,
                run_index=run_index, threads=threads,
            )
            values = self._pump(session)
            if values is _STOP:
                return
            if values.get("usage"):
                self.bus.emit("usage", by_node=values["usage"])
            if values.get("outcome") != "new_task" or not values.get("next_request"):
                return

            threads = dict(values.get("threads", {}))
            task_brief = values["next_request"]
            self.runtime.adopt_new_task(task_brief)
            run_index += 1
            self.bus.emit("status", phase="running", detail=f"New task: {task_brief}")

    def _pump(self, session) -> dict | Any:
        """Run one graph to completion, asking the browser at every interrupt.

        Structurally identical to terminal.drive() -- see this module's header.
        The resume-from-a-cold-checkpoint branch is the same one drive() has:
        after a restart the __interrupt__ payload is gone from the result, so
        the question is reconstructed from state via session.resume_payload.
        """
        graph, config = session.graph, session.config
        self.bus.emit("status", phase=self.phase, title=session.title,
                      thread_id=config["configurable"]["thread_id"])

        snapshot = graph.get_state(config)
        if snapshot.values and snapshot.next:
            payload = session.resume_payload(snapshot.values)
            if payload is not None:
                answer = self._ask(payload, session)
                if answer is _STOP:
                    return _STOP
                result = graph.invoke(Command(resume=answer), config=config)
            else:
                result = graph.invoke(None, config=config)
        else:
            result = graph.invoke(session.initial_state, config=config)

        while True:
            payload = _interrupt_payload(result)
            if payload is None:
                break
            answer = self._ask(payload, session)
            if answer is _STOP:
                return _STOP
            result = graph.invoke(Command(resume=answer), config=config)

        values = graph.get_state(config).values or {}
        self.last_values = values
        self._emit_state(session, values)
        return values

    def _ask(self, payload: dict, session):
        """Publish a question and block this thread until an answer arrives."""
        pending = Pending.from_payload(payload)
        with self._lock:
            self.pending = pending
        self.bus.emit("question", **pending.as_dict(),
                      commands=_offered_commands(session, pending.purpose))
        return self._answers.get()

    # ---- helpers ---------------------------------------------------------

    def _set_phase(self, phase: str, **extra) -> None:
        self.phase = phase
        self.bus.emit("status", phase=phase, **extra)

    def _emit_state(self, session, values: dict) -> None:
        """One snapshot the browser can re-render on."""
        self.bus.emit(
            "state",
            transcript=session.transcript(values),
            outcome=values.get("outcome", ""),
            outputs=values.get("outputs", {}),
            has_agent=self.paths.has_agent(),
        )

    def _drain_answers(self) -> None:
        while True:
            try:
                self._answers.get_nowait()
            except queue.Empty:
                return


def _offered_commands(session, purpose: str) -> list[dict]:
    """The slash commands legal at THIS question, for the browser to offer.

    Computed here rather than in JS for the reason given at the top of
    agent/commands.py: which commands exist depends on the folder's own
    registry, and which of them are legal right now depends on the purpose the
    graph stopped with. `commands.available()` already knows both. A front end
    that hardcoded its own list would drift the moment a designer invented a
    command -- and generated agents invent commands constantly.

    Terminal-scope commands (/help, /state, /graph) are dropped: they are
    handled by the REPL and never reach the graph, and the browser has real UI
    for what they print.
    """
    return [
        {
            "name": command.name,
            "argument": command.argument,
            "summary": command.summary,
            "aliases": list(command.aliases),
        }
        for command in commands.available(purpose or None, session.registry)
        if command.scope != "terminal"
    ]
