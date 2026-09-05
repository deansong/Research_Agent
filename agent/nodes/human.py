"""
WHAT:  The human checkpoint node.  It pauses the graph, shows a question, and
       turns whatever the human typed into a state update + a routing choice.
WHY:   Every path that needs a person goes through here, so the graph has
       exactly one interrupt point and one place that interprets commands.
CONCEPT: interrupt() -- suspend a run until invoke(Command(resume=...)).

This node is also where requirement 2 lives: the ONLY thing that sends the
graph to the planner is a human typing /plan.
"""

from __future__ import annotations

from agent import commands
from agent.state import AgentState, merge_section

from langgraph.types import interrupt


def human_input(state: AgentState):
    """Ask the human something and route on what they say.

    IMPORTANT -- READ BEFORE EDITING:
    On resume, LangGraph re-runs this function from line 1 and the interrupt()
    call returns the resume value instead of pausing.  So everything ABOVE
    interrupt() executes twice and must be free of side effects -- no model
    calls, no writes, no counters.  Everything BELOW it runs exactly once.
    """

    # ---- step 1: describe what we are asking (runs twice; keep it pure) ----
    human = dict(state.get("human", {}))
    purpose = human.get("purpose", "discussion")

    payload = {
        "type": "human_input",
        "purpose": purpose,
        "question": human.get("question", "Your input is required."),
        "context": human.get("context", ""),
    }

    # ---- step 2: STOP HERE.  Everything below runs only after a resume. ----
    answer = str(interrupt(payload)).strip()

    # ---- step 3: interpret the answer --------------------------------------
    # Parsed a second time here (the terminal already parsed it) on purpose:
    # see the "HOW COMMANDS FLOW" comment at the top of agent/commands.py.
    parsed = commands.parse(answer, purpose=purpose)

    if parsed.kind == "command":
        return _handle_command(state, human, purpose, parsed.command.name, parsed.text)

    if parsed.kind == "rejected":
        # The terminal normally filters these out, but a different driver (a
        # test, a web UI) might not.  Re-ask instead of feeding a typo to the
        # model: same purpose, error prepended to the question.
        return {
            "human": merge_section(
                human,
                question=parsed.error + "\n\n" + human.get("question", ""),
            )
        }

    return _handle_text(state, human, purpose, parsed.text)


# ---------------------------------------------------------------------------
# Slash commands that reach the graph
# ---------------------------------------------------------------------------

def _handle_command(state, human: dict, purpose: str, name: str, argument: str) -> dict:
    cycle = int(state.get("task_cycle", 1))

    if name == "exit":
        # _route_after_human sees control.terminate and routes to END.
        return {"control": merge_section(state.get("control"), terminate=True)}

    if name == "new":
        return _start_new_task(state, argument)

    if name == "plan":
        # ---- THE requirement-2 transition ---------------------------------
        # This is the one and only door into the planner.  The planner will
        # read the whole transcript (see state.transcript_for_cycle), so
        # everything discussed up to this moment becomes the plan's input.
        return {
            "transcript": [
                {
                    "role": "system",
                    "text": "The human ended the discussion and asked for a plan."
                    + (f" Extra guidance: {argument}" if argument else ""),
                    "cycle": cycle,
                }
            ],
            "planning": merge_section(state.get("planning"), feedback=argument),
            "human": merge_section(
                human, question="", context="", last_answer="", return_to="planner"
            ),
        }

    if name == "replan":
        reason = argument or (
            "The human asked for a new plan without giving a reason. "
            "Re-examine the repository and the requirements."
        )
        return {
            "planning": merge_section(state.get("planning"), feedback=reason),
            "human": merge_section(
                human, question="", context="", last_answer="", return_to="planner"
            ),
        }

    if name == "discuss":
        return {
            "transcript": [
                {
                    "role": "system",
                    "text": "The human wants to go back to discussing requirements.",
                    "cycle": cycle,
                }
            ],
            "human": merge_section(
                human,
                question="",
                context="",
                last_answer="",
                purpose="discussion",
                return_to="discussor",
            ),
        }

    # A graph-scope command with no branch here means somebody added a
    # registry row and forgot to wire it up.  Fail loudly.
    raise RuntimeError(f"No handler for graph command /{name}")


# ---------------------------------------------------------------------------
# Ordinary answers
# ---------------------------------------------------------------------------

def _handle_text(state: AgentState, human: dict, purpose: str, answer: str) -> dict:
    """Apply a plain-text answer, based on why we asked for it."""
    cycle = int(state.get("task_cycle", 1))

    if purpose == "discussion":
        # Note the shape: we return ONLY the new entry, not the whole list.
        # The operator.add reducer on `transcript` appends it for us.
        # Returning state["transcript"] + [entry] would duplicate everything.
        return {
            "transcript": [{"role": "human", "text": answer, "cycle": cycle}],
            "human": merge_section(
                human, question="", context="", last_answer=answer, return_to="discussor"
            ),
        }

    if purpose == "orchestrator":
        return {
            "human": merge_section(
                human, question="", context="", last_answer=answer, return_to="orchestrator"
            ),
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

    Everything task-specific is cleared, but `providers` (the model
    conversation ids) is carried over: those threads hold accumulated
    knowledge of this repo, and reusing them is what makes the second task
    cheaper than the first.

    `transcript` is NOT cleared -- it cannot be, because its reducer only ever
    appends.  Instead every entry carries a `cycle`, and prompts filter on the
    current one.  Old cycles stay available for /transcript all.
    """
    planning = dict(state.get("planning", {}))
    next_cycle = int(state.get("task_cycle", 1)) + 1

    return {
        "task_cycle": next_cycle,
        "user_request": request,
        "transcript": [{"role": "human", "text": request, "cycle": next_cycle}],
        "discussion": {"requirements": "", "advice": "keep_discussing", "advice_reason": ""},
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
            "last_answer": "",
        },
        "providers": dict(state.get("providers", {})),
        "control": {"event": "", "action": "", "terminate": False},
        "final_summary": "",
    }
