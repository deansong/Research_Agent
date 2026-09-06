"""
WHAT:  The command line entry point and the composition root.
WHY:   One place where every real object is created and wired together.
CONCEPT: How the pieces fit --

    config  ->  agent folder  ->  backends  ->  compiled graph  ->  terminal

The agent itself is now DATA: a folder of JSON describing nodes, prompts and
topology. This file resolves which folder to use, checks it, builds the
backends it asks for, compiles it, and hands it to the REPL.
"""

from __future__ import annotations

import argparse
import contextlib
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from agent import storage
from agent.agentfolder.load import AgentFolderError, load_agent_folder
from agent.agentfolder.validate import format_problems, validate_folder
from agent.backends import build_backends
from agent.backends.base import BackendError
from agent.config import DEFAULT_SESSION, backend_for, describe, load_config
from agent.terminal import drive, print_usage_table
from agent.work.compile import backends_needed, compile_agent
from agent.work.session import work_session
from agent.work.state import initial_work_state


def main() -> None:
    args = _parse_args()

    if args.command == "login":
        from agent.backends.codex import login_chatgpt

        login_chatgpt()
        return

    repo_path = _validated_repo(args.repo)

    if args.command == "promote":
        paths = storage.session_paths(repo_path, args.session or DEFAULT_SESSION)
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
    paths = storage.session_paths(repo_path, cfg.session)

    # ---- 2. which agent are we running? -----------------------------------
    folder_path = _resolve_folder(args, paths, cfg)
    try:
        folder = load_agent_folder(folder_path)
    except AgentFolderError as exc:
        raise SystemExit(f"\nCould not read the agent at {folder_path}:\n{exc}")

    problems = validate_folder(folder)
    blocking = [p for p in problems if not p.warning]
    if problems:
        print(f"\nAgent {folder.graph.name!r} validation:")
        print(format_problems(problems))
    if blocking:
        raise SystemExit("\nThat agent cannot run. Fix the errors above.")

    needed = backends_needed(folder)

    if args.explain:
        _explain(cfg, folder, paths, needed)
        return

    print(describe(cfg))
    print(f"\nagent    {folder.graph.name}  ({folder.path})")
    print(f"session  {paths.session}")

    # ---- 3. backends, checkpointer, graph ---------------------------------
    with contextlib.ExitStack() as stack:
        codex_client = None
        if any(backend_for(cfg, role).provider == "codex" for role in needed):
            from agent.backends.codex import open_client

            codex_client = stack.enter_context(open_client())

        try:
            backends = build_backends(cfg, needed, codex_client=codex_client)
        except BackendError as exc:
            raise SystemExit(f"\n{exc}")

        checkpointer = stack.enter_context(
            SqliteSaver.from_conn_string(str(paths.checkpoint))
        )
        checkpointer.setup()

        _run_work_phase(folder, cfg, paths, backends, checkpointer, args)


def _run_work_phase(folder, cfg, paths, backends, checkpointer, args) -> None:
    """Run the generated agent, once per task.

    The loop exists because /new-style commands end the run with
    outcome="new_task": rather than resetting state in place (which would need
    a "write arbitrary state" primitive in the folder format), we start a FRESH
    thread with the new brief, carrying provider conversations over so the
    prompt-cache saving is preserved.
    """
    from agent.agentfolder.commands import build_registry  # noqa: F401  (docs)

    task_brief = _task_text(args, paths)
    if not task_brief:
        print("No task supplied.")
        return

    threads: dict[str, str] = {}
    run_index = 1

    while True:
        graph = compile_agent(
            folder,
            backends=backends,
            checkpointer=checkpointer,
            registry=__import__(
                "agent.agentfolder.commands", fromlist=["build_registry"]
            ).build_registry(folder),
        )

        # The two phases and each task get their OWN thread id. Measured: two
        # graphs sharing a thread id do not raise -- their channels silently
        # merge, and one schema's keys turn up in the other's state.
        thread_id = f"{cfg.session}:work" + (f":{run_index}" if run_index > 1 else "")

        session = work_session(
            folder=folder,
            graph=graph,
            thread_id=thread_id,
            recursion_limit=cfg.work_recursion_limit,
            initial_state=initial_work_state(
                repo_path=str(paths.repo),
                task_brief=task_brief,
                agent_dir=str(folder.path),
                threads=threads,
            ),
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
        paths.brief.write_text(task_brief)
        run_index += 1
        print(f"\n===== new task in the same session =====\n{task_brief}")


def _report(values: dict) -> None:
    print("\nAgent session ended.")
    if values.get("usage"):
        print_usage_table(values["usage"])


def _resolve_folder(args, paths, cfg) -> Path:
    """--pre-build-agent, else this session's own agent, else the default.

    The middle case is what makes a session resumable: once the bootstrap graph
    has designed an agent and the validator renamed agent.tmp/ to agent/, that
    folder is the durable record of what this session runs.
    """
    if args.pre_build_agent:
        return storage.resolve_agent(args.pre_build_agent, paths)

    if paths.has_agent():
        print(f"Using the agent already designed for this session: {paths.agent_dir}")
        return paths.agent_dir

    # Until the bootstrap graph exists (step 4), fall back to the shipped agent.
    return storage.resolve_agent(cfg.default_agent, paths)


def _task_text(args, paths) -> str:
    """Where the task comes from, in precedence order.

    Normally the bootstrap designer writes brief.md, because it is the thing
    that last had the whole picture. With bootstrap skipped, the human is the
    author -- so ask the human, once, at the CLI. Deliberately NOT from a human
    node inside the agent: that would make a human node mandatory in every
    folder, and would put "what is the task" into the same interrupt vocabulary
    as "the orchestrator has a question".
    """
    if args.task:
        return _remember(paths, args.task.strip())
    if args.task_file:
        return _remember(paths, Path(args.task_file).expanduser().read_text().strip())
    if paths.brief.exists():
        text = paths.brief.read_text().strip()
        if text:
            print(f"Task (from {paths.brief.name}): {text[:100]}")
            return text

    text = input("\nWhat do you want to build/change?\n\nyou> ").strip()
    return _remember(paths, text)


def _remember(paths, text: str) -> str:
    """Persist the brief so a resumed session does not ask again."""
    if text:
        paths.brief.write_text(text)
    return text


def _explain(cfg, folder, paths, needed) -> None:
    """Print everything and spend nothing."""
    print(describe(cfg))
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
    print(_mermaid(folder))


def _mermaid(folder) -> str:
    """Draw the folder's topology without compiling or building backends.

    draw_ascii() would need the `grandalf` package; mermaid needs nothing.
    """
    lines = ["graph TD;", f"  __start__ --> {folder.graph.entry};"]
    for edge in folder.graph.edges:
        lines.append(f"  {edge.from_} --> {edge.to};")
    for branch in folder.graph.branches:
        for case in branch.cases:
            lines.append(f"  {branch.from_} -. {case.when} .-> {case.to};")
        lines.append(f"  {branch.from_} -. default .-> {branch.default};")
    from agent.agentfolder.schema import HumanNodeConfig

    for name, config in folder.nodes.items():
        if isinstance(config, HumanNodeConfig):
            for command in config.commands:
                lines.append(f"  {name} -. /{command.name} .-> {command.to};")
    return "\n".join(lines)


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Interactive LangGraph coding agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py run ./repo --backend fake            # no API calls\n"
            "  python main.py run ./repo --pre-build-agent default --task 'add tests'\n"
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
        help=f"Persistent session name (default: {DEFAULT_SESSION})",
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

    promote = subparsers.add_parser("promote", help="Keep this session's agent for reuse")
    promote.add_argument("repo", help="Path to the repository")
    promote.add_argument("name", help="Name to save it under")
    promote.add_argument("--session", default=None)
    promote.add_argument("--force", action="store_true", help="Replace an existing agent")

    return parser.parse_args()


def _validated_repo(value: str) -> Path:
    repo_path = Path(value).expanduser().resolve()
    if not repo_path.exists():
        raise SystemExit(f"Repository does not exist: {repo_path}")
    if not repo_path.is_dir():
        raise SystemExit(f"Not a directory: {repo_path}")
    return repo_path
