"""
WHAT:  The human checkpoint node.  It pauses the graph, shows a question, and
       turns whatever the human typed into a state update.
WHY:   Every path that needs a person goes through here, so there is exactly
       one interrupt point in the whole graph and one place that interprets
       commands on the graph side.
CONCEPT: interrupt() -- the LangGraph primitive that suspends a run until
       someone calls invoke(Command(resume=...)).
"""

from __future__ import annotations

from langgraph.types import interrupt

from agent import commands
from agent.state import AgentState, merge_section


def human_input(state: AgentState):
    """Ask the human something and route on what they say.

    IMPORTANT -- READ BEFORE EDITING THIS FUNCTION:
    When the graph is resumed, LangGraph re-runs this function from line 1 and
    the interrupt() call below returns the resume value instead of pausing.
    So everything ABOVE interrupt() executes twice (once when we stop, once
    when we resume) and must stay free of side effects: no model calls, no
    file writes, no counters.  Everything BELOW it runs exactly once.
    """

    # ---- step 1: describe what we are asking -------------------------------
    human = dict(state.get("human", {}))
    purpose = human.get("purpose", "discussion")

    payload = {
        "type": "human_input",
        "purpose": purpose,
        "question": human.get("question", "Your input is required."),
        "context": human.get("context", ""),
    }

    # ---- step 2: STOP.  Everything below runs only after a resume. ---------
    answer = str(interrupt(payload)).strip()

    # ---- step 3: interpret the answer --------------------------------------
    # Parsed a second time here (the terminal already parsed it) on purpose --
    # see the "HOW COMMANDS FLOW" comment at the top of agent/commands.py.
    parsed = commands.parse(answer, purpose=purpose)

    if parsed.kind == "command":
        name = parsed.command.name

        if name == "exit":
            # _route_after_human sees control.terminate and routes to END.
            return {"control": merge_section(state.get("control"), terminate=True)}

        if name == "new":
            return _start_new_task(state, parsed.text)

        # A graph-scope command with no handler here is a programming error:
        # somebody added a registry row and forgot to wire it up.
        raise RuntimeError(f"No handler for graph command /{name}")

    if parsed.kind == "rejected":
        # The terminal normally filters these out, but a different driver (a
        # test, a web UI) might not.  Re-ask rather than feeding a typo to the
        # model: keep the same purpose and put the error in the question.
        return {
            "human": merge_section(
                human,
                question=parsed.error + "\n\n" + human.get("question", ""),
            )
        }

    # ---- step 4: a plain-text answer, routed by why we asked ---------------
    return _handle_text(state, human, purpose, parsed.text)


def _handle_text(state: AgentState, human: dict, purpose: str, answer: str) -> dict:
    """Apply an ordinary (non-command) answer, based on why we asked for it."""

    if purpose == "discussion":
        discussion = dict(state.get("discussion", {}))
        history = list(discussion.get("history", []))
        history.append({"question": human.get("question", ""), "answer": answer})

        return {
            "discussion": merge_section(
                discussion,
                history=history,
                last_human_answer=answer,
            ),
            "human": merge_section(human, question="", context="", return_to="discussor"),
        }

    if purpose == "orchestrator":
        return {
            "discussion": merge_section(state.get("discussion"), last_human_answer=answer),
            "human": merge_section(human, question="", context="", return_to="orchestrator"),
            "control": merge_section(state.get("control"), event="human"),
        }

    if purpose == "next_task":
        # The orchestrator finished and asked "what next?", so a plain answer
        # means the same thing as /new <answer>.
        return _start_new_task(state, answer)

    # A purpose we do not recognise is a bug in whichever node set it.
    raise RuntimeError(f"Unknown human purpose: {purpose}")


def _start_new_task(state: AgentState, request: str) -> dict:
    """Reset the workflow for a brand-new task in the same repository.

    Everything task-specific is cleared, but `codex` (the provider thread ids)
    is carried over: those threads hold accumulated knowledge of this repo,
    and reusing them is what makes the second task cheaper than the first.
    """
    planning = dict(state.get("planning", {}))

    return {
        "task_cycle": int(state.get("task_cycle", 1)) + 1,
        "user_request": request,
        "discussion": {
            "history": [],
            "requirements": "",
            "ready": False,
            "last_human_answer": "",
        },
        "planning": {
            "plan": "",
            # generation keeps counting up so executors can tell that the plan
            # they were working from is stale.
            "generation": int(planning.get("generation", 0)),
            "feedback": "",
        },
        "execution": {
            "workstream": "main",
            "current_task": "",
            "result": {},
            "git_status": "",
            "git_diff_stat": "",
        },
        "human": {
            "question": "",
            "context": "",
            "purpose": "discussion",
            "return_to": "discussor",
        },
        "codex": dict(state.get("codex", {})),
        "control": {"event": "", "action": "", "terminate": False},
        "final_summary": "",
    }
