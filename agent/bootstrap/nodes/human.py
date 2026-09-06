"""
WHAT:  The bootstrap graph's human checkpoint.
WHY:   Close to the original agent's, with a fixed command set rather than one
       loaded from JSON -- the bootstrap graph is hand-written and its commands
       are known at import time.
CONCEPT: interrupt(). Everything above it re-runs on resume and must stay pure.
"""

from __future__ import annotations

from langgraph.types import interrupt

from agent import commands as command_lib
from agent.bootstrap.commands import BOOTSTRAP_REGISTRY
from agent.bootstrap.state import BootstrapState
from agent.statelib import merge_section


def human_input(state: BootstrapState):
    human = dict(state.get("human", {}))
    purpose = human.get("purpose", "discussion")

    payload = {
        "type": "human_input",
        "purpose": purpose,
        "question": human.get("question", "Your input is required."),
        "context": human.get("context", ""),
    }

    # ---- STOP. Everything below runs only after a resume. ----------------
    answer = str(interrupt(payload)).strip()

    parsed = command_lib.parse(answer, purpose=purpose, registry=BOOTSTRAP_REGISTRY)

    if parsed.kind == "rejected":
        # Re-ask. Measured: re-entering a node that calls interrupt() produces
        # a FRESH interrupt rather than replaying the old resume value.
        return {"human": merge_section(human, question=parsed.error + "\n\n"
                                       + human.get("question", ""))}

    if parsed.kind == "command":
        return _command(state, human, parsed.command.name, parsed.text)

    # Plain text during discussion.
    return {
        "transcript": [{"role": "human", "text": parsed.text}],
        "human": merge_section(human, question="", context="", last_answer=parsed.text,
                               return_to="discussor"),
    }


def _reread_plan(state, design: dict):
    """Load plan.json, falling back to what the planner produced."""
    import json
    from pathlib import Path

    path = Path(state.get("session_dir", "")) / "plan.json"
    if not path.exists():
        return design.get("plan", {}), ""

    try:
        plan = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return design.get("plan", {}), (
            f"\n[human] {path.name} is not valid JSON ({exc}).\n"
            f"        Using the plan as the planner wrote it instead."
        )

    edited = plan != design.get("plan")
    return plan, (f"\n[human] using your edited {path.name}." if edited else "")


def _command(state, human: dict, name: str, argument: str) -> dict:
    design = dict(state.get("design", {}))

    if name == "approve":
        # Re-read plan.json from disk rather than trusting what the planner
        # returned: the whole point of pausing here is that you can edit the
        # file, and an approval that ignored your edits would be a lie.
        plan, note = _reread_plan(state, design)
        if note:
            print(note)
        return {
            "design": merge_section(design, plan=plan, attempt=0, problems=""),
            "human": merge_section(human, question="", context="", return_to="designer"),
        }

    if name == "revise":
        return {
            "design": merge_section(design, plan_feedback=argument),
            "human": merge_section(human, question="", context="", return_to="planner"),
        }

    if name == "exit":
        return {"outcome": "aborted", "human": merge_section(human, return_to="end")}

    if name == "plan":
        # The one door from discussion into design -- the human's, not a model's.
        return {
            "transcript": [{"role": "human",
                            "text": "Design an agent for this."
                                    + (f" Guidance: {argument}" if argument else "")}],
            "design": merge_section(design, attempt=0, problems=""),
            "human": merge_section(human, question="", context="", last_answer=argument,
                                   return_to="planner"),
        }

    if name == "retry":
        return {
            "design": merge_section(
                design,
                attempt=0,
                problems=(design.get("problems", "") + (
                    f"\n\nThe human also said: {argument}" if argument else "")).strip(),
            ),
            "human": merge_section(human, question="", context="", return_to="designer"),
        }

    if name == "discuss":
        return {
            "human": merge_section(human, question="", context="", last_answer="",
                                   purpose="discussion", return_to="discussor"),
        }

    if name == "use":
        # Fall back to the shipped agent. cli.py sees no session agent and
        # resolves the default.
        print("\n[human] using the built-in default agent.")
        return {"outcome": "ready", "design": merge_section(design, written_to=""),
                "human": merge_section(human, return_to="end")}

    raise RuntimeError(f"No handler for bootstrap command /{name}")
