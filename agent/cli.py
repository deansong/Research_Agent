"""
WHAT:  The command line entry point -- argument parsing, and the terminal's
       half of the conversation.
WHY:   One place that decides what to SAY. What to BUILD lives next door in
       agent/runtime.py, because the web UI needs the same objects and none of
       the same sentences.
CONCEPT: How the pieces fit --

    config  ->  agent folder  ->  backends  ->  compiled graph  ->  terminal
                \\________________ agent/runtime.py ______________/

The agent itself is DATA: a folder of JSON describing nodes, prompts and
topology. `Runtime` resolves which folder to use, checks it, builds the backends
it asks for and compiles it; this file decides when to do that, prints what
happened, and hands the result to the REPL.

If you are reading to learn how a run works, read `runtime.py` first -- it is
the part with no I/O in it.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from agent import storage
from agent.agentfolder.render import mermaid
from agent.agentfolder.validate import format_problems
from agent.backends.base import BackendError
from agent.config import DEFAULT_SESSION, backend_for, describe, load_config
from agent.runtime import needed_for, open_runtime
from agent.terminal import drive, print_usage_table


def main() -> None:
    args = _parse_args()

    if args.command == "login":
        from agent.backends.codex import login_chatgpt

        login_chatgpt()
        return

    repo_path = _validated_repo(args.repo)

    if args.command == "web":
        # Imported here, not at module scope: fastapi and uvicorn are optional
        # extras, and `run` must keep working for someone who never installed
        # them. See requirements.txt.
        try:
            from webui.server import serve
        except ImportError as exc:
            raise SystemExit(
                f"\nThe web UI needs its extra dependencies ({exc.name}).\n"
                f"Install them with:  pip install fastapi 'uvicorn[standard]'"
            )
        serve(repo_path, host=args.host, port=args.port, cli_args=args)
        return

    if args.command == "sessions":
        rows = storage.list_sessions(repo_path)
        if not rows:
            print("No sessions yet.")
            return
        width = max(len(name) for name, _, _ in rows)
        for name, brief, has_agent in rows:
            mark = "agent" if has_agent else "  -  "
            print(f"  {name:<{width}}  {mark}  {brief[:70]}")
        print("\nResume one with:  --session <name>")
        return

    if args.command == "promote":
        paths = storage.session_paths(
            repo_path, args.session or DEFAULT_SESSION, session_dir=args.session_dir
        )
        target = storage.promote(paths, args.name, overwrite=args.force)
        print(f"Promoted this session's agent to {target}")
        print("It is not gitignored -- commit it to keep it.")
        return

    # ---- 1. configuration -------------------------------------------------
    cfg = load_config(
        repo_path=repo_path,
        config_file=Path(args.config).expanduser() if args.config else None,
        cli=args,
    )
    # The task decides the session, not the other way round. A session owns one
    # task: its conversation, the agent designed from that conversation, and
    # the brief that agent runs. Defaulting every run to one shared session
    # meant a second, unrelated task silently reused the first task's agent.
    paths, task_brief = _resolve_session(args, cfg, repo_path)

    # ---- 2. open the checkpointer -----------------------------------------
    # Both phases share one database. They MUST use different thread ids --
    # measured: two graphs with different schemas on the same thread do not
    # raise, their channels silently merge and one schema's keys turn up in
    # the other's state. Runtime owns that rule; see agent/runtime.py.
    with open_runtime(cfg, paths) as runtime:

        # ---- 3. phase 1: design an agent, unless we already have one -------
        folder_path = runtime.resolve_folder(args.pre_build_agent)
        if folder_path is not None:
            if args.pre_build_agent:
                print(f"Skipping the design phase: using {folder_path}")
            else:
                print(f"Reusing the agent designed for this session: {folder_path}")

        if folder_path is None:
            if args.explain:
                print(describe(cfg, paths))
                print("\nNo agent has been designed for this session yet.")
                print("Run without --explain to design one, or pass --pre-build-agent.")
                return
            print(describe(cfg, paths))
            if not _bootstrap(runtime, cfg, paths, task_brief):
                return
            folder_path = runtime.resolve_folder(None) or runtime.fallback_folder()

        # ---- 4. load and check the agent -----------------------------------
        loaded = runtime.load_folder(folder_path)
        if loaded.folder is None:
            raise SystemExit(
                f"\nCould not read the agent at {folder_path}:\n{loaded.error}"
            )

        folder = loaded.folder
        if loaded.problems:
            print(f"\nAgent {folder.graph.name!r} validation:")
            print(format_problems(loaded.problems))
        if loaded.blocking:
            raise SystemExit("\nThat agent cannot run. Fix the errors above.")

        needed = needed_for(folder)

        if args.explain:
            _explain(cfg, folder, paths, needed)
            return

        print(f"\nagent    {folder.graph.name}  ({folder.path})")

        # ---- 5. phase 2: run it --------------------------------------------
        try:
            backends = runtime.backends(needed)
        except BackendError as exc:
            raise SystemExit(f"\n{exc}")

        try:
            _run_work_phase(runtime, folder, cfg, paths, backends, task_brief)
        except BackendError as exc:
            # A provider problem, not a bug. The graph checkpoints after every
            # completed node, so whatever finished before this is still there --
            # say so, and say exactly how to pick it up, instead of printing a
            # traceback that buries both facts.
            raise SystemExit(_recoverable(exc, paths, folder))
        except KeyboardInterrupt:
            raise SystemExit(
                f"\nStopped.\n\nResume with:\n"
                f"  python main.py run {paths.repo} --session {paths.session.name}"
            )


def _bootstrap(runtime, cfg, paths, request) -> bool:
    """Phase 1, and the terminal's commentary on it.

    Returns True if an agent is ready to run. Everything about HOW the design
    graph is built lives in Runtime.bootstrap_session(); what is left here is
    the two sentences a terminal user wants to see.
    """
    if not request:
        print("No task supplied.")
        return False

    try:
        session = runtime.bootstrap_session(request)
    except BackendError as exc:
        raise SystemExit(f"\n{exc}")

    values = drive(session, cfg)

    if values.get("outcome") != "ready":
        print("\nStopped before an agent was ready.")
        return False

    brief = runtime.save_brief(values)
    if brief:
        print("\n===== the brief handed to the new agent =====")
        print(brief)

    return True


def _resolve_session(args, cfg, repo_path):
    """Return (SessionPaths, task text).

    Three routes in:
      --session-dir P  you chose the folder. It may already contain a
                       hand-written brief.txt, which IS the task.
      --session NAME   you named it, so you mean "carry on with that one".
                       The task may come from its saved request.
      neither          the task names the session, so the same task resumes
                       and a different task starts somewhere clean.

    Note the asymmetry, which is not an oversight: the first two know the
    folder BEFORE they know the task, so they can read a file out of it. The
    third derives the folder FROM the task, so there is nowhere to look yet --
    which is exactly why preparing a brief.txt requires naming the folder.
    """
    supplied = args.task.strip() if args.task else (
        Path(args.task_file).expanduser().read_text().strip() if args.task_file else ""
    )
    session_dir = getattr(args, "session_dir", None)

    if session_dir or cfg.session:
        paths = storage.session_paths(
            repo_path, cfg.session or "unnamed", session_dir=session_dir
        )
        # Precedence, most explicit first. brief.txt outranks request.txt
        # because YOU wrote brief.txt and WE wrote request.txt: if the two
        # disagree, you edited the brief and meant it. (The guard below then
        # stops that quietly re-running an agent designed for the old text.)
        task = (
            supplied
            or _hand_written_brief(paths)
            or _saved_request(paths)
            or _prompt_for_task()
        )
        _warn_if_task_changed(paths, task)
    else:
        task = supplied or _prompt_for_task()
        if not task:
            return storage.session_paths(repo_path, "unnamed"), ""
        paths = storage.session_paths(repo_path, storage.session_name_for(task))
        print(f"session  {paths.session.name}  (derived from the task; "
              f"reuse it with --session {paths.session.name})")

    if task:
        # request.txt is what YOU asked for and is what identifies the session.
        # brief.md is the designer's rewrite of it for the generated agent --
        # comparing against that would falsely trip the guard below, because
        # the designer legitimately rewords the task.
        paths.request.write_text(task)
    return paths, task


def _hand_written_brief(paths) -> str:
    """The initial idea, if you left one in the session folder as brief.txt.

    This is the whole "prepare a folder, then run it" workflow: write the idea
    into a file at your leisure, in an editor, with paragraphs -- rather than
    typing it into a one-line terminal prompt or quoting it on a command line
    where a newline ends the argument.
    """
    if not paths.input_brief.exists():
        return ""
    text = paths.input_brief.read_text().strip()
    if not text:
        # An empty file is almost certainly "I meant to write this and did
        # not", so say so rather than silently falling through to the prompt.
        print(f"Note: {paths.input_brief} is empty; ignoring it.")
        return ""
    print(f"Task (from {paths.input_brief.name}):")
    print(_indent(text))
    return text


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _saved_request(paths) -> str:
    if not paths.request.exists():
        return ""
    text = paths.request.read_text().strip()
    if text:
        print(f"Task (from {paths.request.name}): {text[:100]}")
    return text


def _prompt_for_task() -> str:
    return input("\nWhat do you want to build/change?\n\nyou> ").strip()


def _warn_if_task_changed(paths, task: str) -> None:
    """Refuse to silently run one task's agent against a different task.

    Only reachable when YOU named the folder -- --session or --session-dir --
    since a name derived from the task cannot collide across different tasks.
    Worth an explicit stop: the agent in a session was designed FOR its brief,
    and pointing it at unrelated work produces confident, plausible, wrong
    output. Editing brief.txt after an agent exists lands here too, which is
    the point: it is the same mistake, made in a file instead of a flag.
    """
    if not paths.has_agent() or not paths.request.exists():
        return

    previous = paths.request.read_text().strip()
    if not previous or previous == task.strip():
        return

    # The first suggestion depends on how you got here, because "drop
    # --session" is no help at all to someone who passed --session-dir.
    start_fresh = (
        "  - use an empty folder: --session-dir <new path>;\n"
        if paths.external else
        f"  - drop --session, and a new session will be derived from this task;\n"
        f"  - pass --session {storage.session_name_for(task)} to start one explicitly;\n"
    )

    raise SystemExit(
        f"\nSession {paths.session.name!r} already has an agent, designed for:\n"
        f"    {previous[:200]}\n\n"
        f"You are asking it to do something else:\n"
        f"    {task[:200]}\n\n"
        f"That agent was built for the first task and would likely do the wrong "
        f"thing.\nEither:\n"
        f"{start_fresh}"
        f"  - or delete {paths.agent_dir} to redesign in place\n"
        f"    (that keeps the folder, its brief.txt and its artifacts -- only "
        f"the agent is rebuilt)."
    )


def _run_work_phase(runtime, folder, cfg, paths, backends, task_brief) -> None:
    """Run the generated agent, once per task.

    The loop exists because /new-style commands end the run with
    outcome="new_task": rather than resetting state in place (which would need
    a "write arbitrary state" primitive in the folder format), we start a FRESH
    thread with the new brief, carrying provider conversations over so the
    prompt-cache saving is preserved.
    """
    if not task_brief:
        print("No task supplied.")
        return

    _, plan_warning = runtime.load_plan_reporting()
    if plan_warning:
        print(f"Warning: {plan_warning}")

    threads: dict[str, str] = {}
    run_index = 1

    while True:
        session = runtime.work_session(
            folder,
            backends=backends,
            task_brief=task_brief,
            run_index=run_index,
            threads=threads,
        )

        try:
            values = drive(session, cfg)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "GraphRecursionError":
                raise SystemExit(
                    f"\nThe agent ran {cfg.work_recursion_limit} steps without finishing.\n"
                    f"That usually means its graph has a loop with no way out.\n"
                    f"Look at it with:  python main.py run . --explain "
                    f"--pre-build-agent {folder.path}"
                ) from None
            raise

        _report(values)

        if values.get("outcome") != "new_task" or not values.get("next_request"):
            break

        # Carry conversations forward so the next task is cheap.
        threads = dict(values.get("threads", {}))
        task_brief = values["next_request"]

        runtime.adopt_new_task(task_brief)
        run_index += 1
        print(f"\n===== new task, same agent =====\n{task_brief}")
        print("(this agent was designed for the previous task -- /exit and "
              "start a new session if it does not fit)")


def _recoverable(exc: BackendError, paths, folder) -> str:
    """The message a provider failure should end with.

    Two things the user needs and a traceback does not give them: what went
    wrong in one sentence, and the exact command to carry on.
    """
    return (
        f"\n{exc}\n\n"
        f"Nothing is lost -- every completed step is checkpointed.\n"
        f"Resume with:\n"
        f"  python main.py run {paths.repo} --session {paths.session.name}\n\n"
        f"(that reuses the agent already designed for this session, at\n"
        f" {folder.path})"
    )


def _report(values: dict) -> None:
    print("\nAgent session ended.")
    if values.get("usage"):
        print_usage_table(values["usage"])


def _explain(cfg, folder, paths, needed) -> None:
    """Print everything and spend nothing."""
    print(describe(cfg, paths))
    print()
    print(f"agent        {folder.graph.name}")
    print(f"  from       {folder.path}")
    print(f"  {folder.graph.description}")
    print()
    print("nodes:")
    for ref in folder.graph.nodes:
        config = folder.nodes[ref.name]
        extra = ""
        if hasattr(config, "backend"):
            extra = f"backend={config.backend} access={config.access}"
        print(f"  {ref.name:16} {ref.kind:8} {extra}")
    print()
    print("backends this agent needs:")
    for role, access in needed.items():
        spec = backend_for(cfg, role)
        print(f"  {role:16} {spec.provider:12} {spec.model or '(default)':20} needs {access.value}")
    print()
    print(f"session      {paths.session}")
    print(f"checkpoint   {paths.checkpoint}")
    print()
    print("topology (mermaid):")
    print(mermaid(folder))


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Interactive LangGraph coding agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py run ./repo --backend fake            # no API calls\n"
            "  python main.py run ./repo --pre-build-agent default --task 'add tests'\n"
            "  python main.py run ./repo --session-dir ~/exp/run1  # session folder of your choosing\n"
            "  python main.py web ./repo                           # the browser UI\n"
            "  python main.py run ./repo --explain                 # show everything, spend nothing\n"
            "  python main.py promote ./repo my-reviewer           # keep this session's agent\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("login", help="Sign into Codex using ChatGPT")

    run = subparsers.add_parser("run", help="Run the coding agent")
    run.add_argument("repo", help="Path to the repository")
    run.add_argument(
        "--session",
        # Default None, not DEFAULT_SESSION: an argparse default is
        # indistinguishable from something the user typed, so a non-None
        # default would silently outrank the config file.
        default=None,
        help="Resume a named session (default: one derived from the task)",
    )
    run.add_argument(
        "--session-dir", metavar="PATH",
        help="Use this directory as the session folder, instead of "
             "<repo>/.agent/sessions/<name>. If it holds a brief.txt, that is "
             "read as the task.",
    )
    run.add_argument("--pre-build-agent", metavar="NAME|PATH",
                     help="Skip designing an agent; run this one")
    run.add_argument("--task", help="The task, instead of being prompted")
    run.add_argument("--task-file", help="Read the task from a file")

    run.add_argument("--config", help="Path to a config JSON file (replaces the default files)")
    run.add_argument("--backend", help="Provider for every role, e.g. codex, fake")
    run.add_argument("--model", help="Model for every role")
    run.add_argument("--backend-role", action="append", metavar="ROLE=PROVIDER[:MODEL]",
                     help="Override one role, e.g. executor=codex:gpt-5.4. Repeatable.")
    run.add_argument("--explain", action="store_true",
                     help="Print the resolved config and the agent, then exit")

    web = subparsers.add_parser("web", help="Serve the browser UI")
    web.add_argument("repo", help="Path to the repository")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8420)
    # The same backend flags `run` takes, so `web . --backend fake` means what
    # you would expect. They become the defaults for every session the UI opens.
    web.add_argument("--config", help="Path to a config JSON file")
    web.add_argument("--backend", help="Provider for every role, e.g. codex, fake")
    web.add_argument("--model", help="Model for every role")
    web.add_argument("--backend-role", action="append", metavar="ROLE=PROVIDER[:MODEL]",
                     help="Override one role. Repeatable.")

    sessions = subparsers.add_parser("sessions", help="List this repo's sessions")
    sessions.add_argument("repo", help="Path to the repository")

    promote = subparsers.add_parser("promote", help="Keep this session's agent for reuse")
    promote.add_argument("repo", help="Path to the repository")
    promote.add_argument("name", help="Name to save it under")
    promote.add_argument("--session", default=None)
    promote.add_argument("--session-dir", default=None, metavar="PATH",
                         help="Promote from a session folder outside .agent/sessions/")
    promote.add_argument("--force", action="store_true", help="Replace an existing agent")

    return parser.parse_args()


def _validated_repo(value: str) -> Path:
    repo_path = Path(value).expanduser().resolve()
    if not repo_path.exists():
        raise SystemExit(f"Repository does not exist: {repo_path}")
    if not repo_path.is_dir():
        raise SystemExit(f"Not a directory: {repo_path}")
    return repo_path
