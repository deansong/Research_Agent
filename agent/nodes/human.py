from __future__ import annotations

from langgraph.types import interrupt

from agent.state import AgentState, merge_section



def human_input(state: AgentState):
    """Human checkpoint. Keep this node free of LLM calls before interrupt()."""

    human = dict(state.get("human", {}))
    payload = {
        "type": "human_input",
        "purpose": human.get("purpose", "discussion"),
        "question": human.get("question", "Your input is required."),
        "context": human.get("context", ""),
    }

    answer = str(interrupt(payload)).strip()

    if answer.lower() in {"/quit", "/exit", "quit", "exit"}:
        return {
            "control": merge_section(state.get("control"), terminate=True),
        }

    purpose = human.get("purpose", "discussion")

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
            "discussion": merge_section(
                state.get("discussion"),
                last_human_answer=answer,
            ),
            "human": merge_section(human, question="", context="", return_to="orchestrator"),
            "control": merge_section(state.get("control"), event="human"),
        }

    if purpose == "next_task":
        planning = dict(state.get("planning", {}))
        codex_state = dict(state.get("codex", {}))

        return {
            "task_cycle": int(state.get("task_cycle", 1)) + 1,
            "user_request": answer,
            "discussion": {
                "history": [],
                "requirements": "",
                "ready": False,
                "last_human_answer": "",
            },
            "planning": {
                "plan": "",
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
            "codex": codex_state,
            "control": {
                "event": "",
                "action": "",
                "terminate": False,
            },
            "final_summary": "",
        }

    raise RuntimeError(f"Unknown human purpose: {purpose}")
