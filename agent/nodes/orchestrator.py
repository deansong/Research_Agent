"""
WHAT:  The orchestrator node -- decides what happens after every step.
WHY:   Somebody has to answer "is this done?", "does this need another coding
       task?", "should we replan?". Splitting that from the planner means the
       planner is not re-reading the whole repo on every loop.
CONCEPT: A node that drives a CONDITIONAL EDGE. It writes control.action, and
       graph.py::_route_after_orchestrator turns that into a destination.
       Note the split: the node DECIDES, the router only READS. Keeping
       routers free of logic is what makes the graph readable.
"""

from __future__ import annotations

import json

from agent.backends.base import Access
from agent.prompts import ORCHESTRATOR_INSTRUCTIONS
from agent.schemas import OrchestratorOutput
from agent.state import AgentState, merge_section
from agent.telemetry import record_usage


def make_orchestrator(backend):
    def orchestrator(state: AgentState):
        print("\n[orchestrator] deciding next action...")

        providers = dict(state.get("providers", {}))
        role_threads = dict(providers.get("role_threads", {}))
        thread_id = role_threads.get("orchestrator")

        control = dict(state.get("control", {}))
        event = control.get("event", "planner")
        discussion = dict(state.get("discussion", {}))
        planning = dict(state.get("planning", {}))
        execution = dict(state.get("execution", {}))

        if event == "planner":
            prompt = f"""
A planner has produced an implementation plan.

Requirements:
{discussion.get('requirements', '')}

Plan:
{planning.get('plan', '')}

Planner's proposed first task:
{execution.get('current_task', '')}

Proposed workstream:
{execution.get('workstream', 'main')}

Decide what should happen next.
"""
        elif event == "executor":
            prompt = f"""
The coding worker has completed its latest turn.

Executor result:
{json.dumps(execution.get('result', {}), indent=2)}

Current git status:
{execution.get('git_status', '')}

Current diff summary:
{execution.get('git_diff_stat', '')}

Decide the next action. If more coding is required, provide one concrete next task.
"""
        elif event == "human":
            prompt = f"""
The human answered your question:

{state.get('human', {}).get('last_answer', '')}

Continue orchestration based on that answer and choose the next action.
"""
        else:
            prompt = "Review the current task state and decide the next action."

        run = backend.run_structured(
            thread_id=thread_id,
            repo_path=state["repo_path"],
            access=Access.READ_ONLY,
            developer_instructions=ORCHESTRATOR_INSTRUCTIONS,
            prompt=prompt,
            output_model=OrchestratorOutput,
        )

        role_threads["orchestrator"] = run.thread_id
        providers["role_threads"] = role_threads
        usage = record_usage(state.get("usage_by_role", {}), "orchestrator", run.usage)
        action = run.data.action

        print(f"\n[orchestrator] action = {action}")

        # Shared by every branch below.
        base = {
            "providers": providers,
            "usage_by_role": usage,
        }

        if action == "execute":
            task = run.data.task.strip() or execution.get("current_task", "").strip()
            if not task:
                task = "Continue with the next unfinished plan item."

            workstream = run.data.workstream.strip() or execution.get("workstream", "main")
            print("[orchestrator] executor task:", task)

            return {
                **base,
                "execution": merge_section(
                    execution,
                    current_task=task,
                    workstream=workstream,
                ),
                "control": merge_section(control, action="execute"),
            }

        if action == "replan":
            feedback = run.data.feedback.strip() or (
                "Reconsider the plan based on the latest execution result and repository state."
            )
            return {
                **base,
                "planning": merge_section(planning, feedback=feedback),
                "control": merge_section(control, action="replan"),
            }

        if action == "ask_human":
            question = run.data.question.strip() or (
                "A decision is required before implementation can continue. What should we do?"
            )
            return {
                **base,
                "human": merge_section(
                    state.get("human"),
                    question=question,
                    context=run.data.feedback.strip(),
                    purpose="orchestrator",
                    return_to="orchestrator",
                ),
                "control": merge_section(control, action="ask_human"),
            }

        summary = run.data.final_summary.strip() or execution.get("result", {}).get(
            "summary", "Task completed."
        )
        print("\n======================================")
        print("TASK COMPLETE")
        print("======================================")
        print(summary)

        return {
            **base,
            "final_summary": summary,
            "human": merge_section(
                state.get("human"),
                question="Enter another request for this project, or /exit to stop.",
                context=summary,
                purpose="next_task",
                return_to="discussor",
            ),
            "control": merge_section(control, action="finish"),
        }

    return orchestrator
