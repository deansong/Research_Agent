"""
WHAT:  The command line entry point and the composition root.
WHY:   One place where every real object is created and wired together.
       Nothing else in the project constructs a backend or a checkpointer.
CONCEPT: compile(checkpointer=...) -- the checkpointer opened here is what
       makes a session resumable.

Read this file to see how the pieces fit:

    config  ->  backends  ->  graph  ->  terminal loop
"""

from __future__ import annotations

import argparse
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from openai_codex import Codex

from agent.backends import build_backends
from agent.backends.base import BackendError
from agent.config import DEFAULT_SESSION, describe, load_config
from agent.graph import build_graph
from agent.storage import database_path
from agent.terminal import drive_graph


def main() -> None:
    args = _parse_args()

    if args.command == "login":
        _login_chatgpt()
        return

    repo_path = _validated_repo(args.repo)

    # ---- 1. resolve configuration ------------------------------------------
    cfg = load_config(
        repo_path=repo_path,
        config_file=Path(args.config).expanduser() if args.config else None,
        cli=args,
    )

    if args.explain:
        # Print everything and spend nothing. The fastest way to answer
        # "which model is my executor actually using?".
        print(describe(cfg))
        print()
        print(f"checkpoint database: {database_path(repo_path)}")
        print()
        print(_TOPOLOGY)
        return

    print(describe(cfg))

    db_path = database_path(repo_path)
    print(f"\nLangGraph state: {db_path}")

    # ---- 2. build the backends ---------------------------------------------
    # The Codex client is opened here, as a context manager, and shared by
    # every role configured to use Codex. Roles on other providers ignore it.
    with Codex() as codex:
        try:
            backends = build_backends(cfg, codex_client=codex)
        except BackendError as exc:
            # A configuration problem. Stop now, before spending anything.
            raise SystemExit(f"\n{exc}")

        # ---- 3. open the checkpointer and build the graph -------------------
        with SqliteSaver.from_conn_string(str(db_path)) as checkpointer:
            checkpointer.setup()
            graph = build_graph(backends, checkpointer)

            # ---- 4. run the interactive loop --------------------------------
            drive_graph(graph, repo_path, cfg)


_TOPOLOGY = """GRAPH
------------------------------------------------------------------------------
  START -> discussor -> human -+- plain text -> discussor
                               +- /plan       -> planner -> orchestrator
                               +- /replan     -> planner
                               +- /discuss    -> discussor
                               +- /exit       -> END

  orchestrator -+- execute   -> executor -> orchestrator
                +- replan    -> planner
                +- ask_human -> human
                +- finish    -> human  (asks you what to do next)

  The discussor has NO edge to the planner: only /plan gets you there."""


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Interactive LangGraph coding agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py run ./myrepo --session feature-x\n"
            "  python main.py run ./myrepo --backend fake        # no API calls\n"
            "  python main.py run ./myrepo --backend-role executor=codex:gpt-5.4\n"
            "  python main.py run ./myrepo --explain             # show config, spend nothing\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("login", help="Sign into Codex using ChatGPT")

    run = subparsers.add_parser("run", help="Run the coding agent")
    run.add_argument("repo", help="Path to the repository")
    run.add_argument(
        "--session",
        # Default is None, NOT DEFAULT_SESSION: an argparse default is
        # indistinguishable from something the user typed, so a non-None
        # default here would silently outrank the "session" key in a config
        # file. config.load_config() supplies the real default.
        default=None,
        help=f"Persistent session name (default: {DEFAULT_SESSION})",
    )

    # ---- configuration overrides (highest precedence -- see config.py) -----
    run.add_argument("--config", help="Path to a config JSON file (replaces the default files)")
    run.add_argument("--backend", help="Provider for every role, e.g. codex, fake")
    run.add_argument("--model", help="Model for every role")
    run.add_argument(
        "--backend-role",
        action="append",
        metavar="ROLE=PROVIDER[:MODEL]",
        help="Override one role, e.g. executor=codex:gpt-5.4. Repeatable.",
    )
    run.add_argument(
        "--explain",
        action="store_true",
        help="Print the resolved config and graph topology, then exit",
    )

    return parser.parse_args()


def _login_chatgpt() -> None:
    with Codex() as codex:
        login = codex.login_chatgpt()
        print("\nOpen this URL in your browser:")
        print(login.auth_url)
        print()
        login.wait()
        print("ChatGPT/Codex login successful.")


def _validated_repo(value: str) -> Path:
    repo_path = Path(value).expanduser().resolve()
    if not repo_path.exists():
        raise SystemExit(f"Repository does not exist: {repo_path}")
    if not repo_path.is_dir():
        raise SystemExit(f"Not a directory: {repo_path}")
    return repo_path
