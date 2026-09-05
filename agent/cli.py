from __future__ import annotations

import argparse
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from openai_codex import Codex

from agent.codex_backend import CodexBackend
from agent.config import CODEX_MODEL, DEFAULT_SESSION
from agent.graph import build_graph
from agent.storage import database_path
from agent.terminal import drive_graph



def main() -> None:
    args = _parse_args()

    if args.command == "login":
        _login_chatgpt()
        return

    repo_path = _validated_repo(args.repo)
    db_path = database_path(repo_path)
    print(f"LangGraph state: {db_path}")

    with Codex() as codex:
        backend = CodexBackend(codex, model=CODEX_MODEL)
        with SqliteSaver.from_conn_string(str(db_path)) as checkpointer:
            checkpointer.setup()
            graph = build_graph(backend, checkpointer)
            drive_graph(graph, repo_path, args.session)


def _parse_args():
    parser = argparse.ArgumentParser(description="Interactive LangGraph + Codex coding agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("login", help="Sign into Codex using ChatGPT")

    run = subparsers.add_parser("run", help="Run the coding agent")
    run.add_argument("repo", help="Path to the repository")
    run.add_argument("--session", default=DEFAULT_SESSION, help="Persistent LangGraph session name")

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
