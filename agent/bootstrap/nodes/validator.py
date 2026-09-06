"""
WHAT:  Checks the written folder, and on success promotes agent.tmp/ to agent/.
WHY:   Validating from DISK rather than from the in-memory proposal proves the
       folder round-trips through JSON -- which catches things the in-memory
       object cannot show, like a duplicate key collapsing in nodes.json.
CONCEPT: A node with no model in it, that drives a conditional edge. It writes
       control decisions into state; graph.py's router only reads them.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from agent.agentfolder.load import AgentFolderError, load_agent_folder
from agent.agentfolder.validate import format_problems, validate_folder
from agent.bootstrap.state import BootstrapState
from agent.statelib import merge_section


def make_validator(paths, *, max_attempts: int):
    def validator(state: BootstrapState):
        design = dict(state.get("design", {}))
        attempt = int(design.get("attempt", 0))
        staging = Path(design.get("written_to") or paths.staging)

        problems_text = design.get("problems", "").strip()

        if not problems_text:
            problems_text = _check(staging)

        if not problems_text:
            # Success. The rename is what makes this durable -- see writer.py.
            target = Path(paths.agent_dir)
            if target.exists():
                shutil.rmtree(target)
            staging.rename(target)
            print(f"\n[validator] the design is valid. Agent written to {target}")
            return {
                "design": merge_section(design, written_to=str(target), problems=""),
                "outcome": "ready",
            }

        print(f"\n[validator] the design has problems:\n{problems_text}")

        if attempt < max_attempts:
            print(f"[validator] asking the designer to fix them "
                  f"(attempt {attempt + 1} of {max_attempts})")
            return {"design": merge_section(design, problems=problems_text)}

        print(f"[validator] gave up after {max_attempts} attempts.")
        return {
            "design": merge_section(design, problems=problems_text),
            "human": merge_section(
                state.get("human"),
                purpose="design_failed",
                question=(
                    f"I could not produce a working agent after {max_attempts} attempts.\n"
                    f"/retry to try again, /discuss to talk it through, "
                    f"/use to run the built-in default agent, or /exit."
                ),
                context=problems_text,
                return_to="human",
            ),
        }

    return validator


def _check(staging: Path) -> str:
    """Load and validate from disk. Returns "" when the folder is good."""
    if not (staging / "graph.json").exists():
        return "The designer did not produce a folder."

    try:
        folder = load_agent_folder(staging)
    except AgentFolderError as exc:
        return f"The folder could not be read:\n{exc}"

    problems = [p for p in validate_folder(folder) if not p.warning]
    if problems:
        return format_problems(problems)

    # Final smoke test: it must actually compile. Backends are not built yet,
    # so this catches structural problems only -- which is exactly what is
    # left after validate_folder has run.
    try:
        from unittest.mock import MagicMock

        from agent.agentfolder.commands import build_registry
        from agent.work.compile import backends_needed, compile_agent

        compile_agent(
            folder,
            backends={role: MagicMock() for role in backends_needed(folder)},
            checkpointer=None,
            registry=build_registry(folder),
        )
    except Exception as exc:  # noqa: BLE001
        return f"The folder is well-formed but would not compile: {exc}"

    return ""
