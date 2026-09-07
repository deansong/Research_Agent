"""
WHAT:  The bootstrap graph's human checkpoint.
WHY:   Close to the original agent's, with a fixed command set rather than one
       loaded from JSON -- the bootstrap graph is hand-written and its commands
       are known at import time.
CONCEPT: interrupt(). Everything above it re-runs on resume and must stay pure.
"""

from __future__ import annotations

from pathlib import Path

from langgraph.types import interrupt

from agent import commands as command_lib
from agent.bootstrap.commands import BOOTSTRAP_REGISTRY
from agent.bootstrap.state import BootstrapState
from agent.statelib import merge_section


def make_human(paths):
    """The human node, bound to this session's paths.

    A factory rather than a bare function because approving a design has to
    leave something on DISK -- see SessionPaths.is_approved. Same shape as
    make_planner and make_validator.
    """

    def human_input(state: BootstrapState):
        return _human_input(state, paths)

    return human_input


def _human_input(state: BootstrapState, paths):
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
        return _command(state, human, parsed.command.name, parsed.text, paths)

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


def _approve_design(state, design: dict, human: dict, paths) -> dict:
    """Start executing -- but only if what is on disk still works.

    Re-validating here is the whole reason the gate is worth having. Between
    the validator writing the folder and you typing /approve you may have
    rewired the graph, split a node or edited a prompt, and any of those can
    break it. Approving without re-reading would hand a broken agent to the
    work phase, where the failure arrives with much less context.

    A refusal keeps you parked at the same question rather than unwinding
    anything, so the fix is one more edit away.
    """
    from agent.agentfolder.load import AgentFolderError, load_agent_folder
    from agent.agentfolder.validate import format_problems, validate_folder

    folder_path = Path(design.get("written_to") or "")
    try:
        folder = load_agent_folder(folder_path)
    except AgentFolderError as exc:
        print(f"\n[human] that agent cannot be read:\n{exc}")
        return {
            "human": merge_section(
                human,
                question=("The agent on disk cannot be read. Fix it and "
                          "/approve again, or /retry to design a new one."),
                context=str(exc),
                return_to="human",
            ),
        }

    blocking = [p for p in validate_folder(folder) if not p.warning]
    if blocking:
        report = format_problems(blocking)
        print(f"\n[human] that agent will not run:\n{report}")
        return {
            "human": merge_section(
                human,
                question=("The agent has problems that stop it running. Fix "
                          "them and /approve again, or /retry."),
                context=report,
                return_to="human",
            ),
        }

    # Durable, because the gate lives in this graph but the decision to run
    # is read by cli.py and the web server on every later start.
    paths.approve(f"approved {folder.graph.name} with {len(folder.graph.nodes)} nodes\n")
    print("\n[human] approved. Running the agent.")
    return {
        "transcript": [{"role": "human", "text": "Approved the designed agent."}],
        "outcome": "ready",
        "human": merge_section(human, question="", context="", return_to="end"),
    }


def _command(state, human: dict, name: str, argument: str, paths) -> dict:
    design = dict(state.get("design", {}))

    if name == "approve":
        # /approve means "accept what is in front of me", and there are two
        # things it can be in front of. Both re-read from DISK rather than
        # trusting what the model returned, for the same reason: the point of
        # pausing is that you can edit the file, and an approval that ignored
        # your edits would be a lie.
        if human.get("purpose") == "design_review":
            return _approve_design(state, design, human, paths)

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
