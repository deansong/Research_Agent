from __future__ import annotations

from openai_codex import Sandbox

from agent.codex_backend import CodexBackend
from agent.prompts import PLANNER_INSTRUCTIONS
from agent.schemas import PlannerOutput
from agent.state import AgentState, merge_section
from agent.telemetry import record_usage



def make_planner(backend: CodexBackend):
    def planner(state: AgentState):
        print("\n[planner] preparing implementation plan...")

        codex_state = dict(state.get("codex", {}))
        role_threads = dict(codex_state.get("role_threads", {}))
        thread_id = role_threads.get("planner")

        planning = dict(state.get("planning", {}))
        feedback = planning.get("feedback", "").strip()
        requirements = state.get("discussion", {}).get("requirements", "")

        if feedback:
            prompt = f"""
Revise your current implementation plan.

The orchestrator's feedback is:

{feedback}

Inspect the repository again if necessary. Return the revised plan,
workstream, and next executable task.
"""
        else:
            prompt = f"""
A new finalized requirement set is ready.

Task #{state.get('task_cycle', 1)}

Requirements:

{requirements}

Inspect the repository and create an implementation plan. Return the plan,
the most appropriate workstream, and the first concrete executor task.
"""

        run = backend.run_structured(
            thread_id=thread_id,
            repo_path=state["repo_path"],
            sandbox=Sandbox.read_only,
            developer_instructions=PLANNER_INSTRUCTIONS,
            prompt=prompt,
            output_model=PlannerOutput,
        )

        role_threads["planner"] = run.thread_id
        codex_state["role_threads"] = role_threads
        usage = record_usage(state.get("usage_by_role", {}), "planner", run.usage)

        generation = int(planning.get("generation", 0)) + 1
        print("\n[planner] plan:")
        print(run.data.plan)
        print("\n[planner] first task:", run.data.next_task)

        return {
            "codex": codex_state,
            "usage_by_role": usage,
            "planning": merge_section(
                planning,
                plan=run.data.plan.strip(),
                generation=generation,
                feedback="",
            ),
            "execution": merge_section(
                state.get("execution"),
                workstream=run.data.workstream.strip() or "main",
                current_task=run.data.next_task.strip(),
            ),
            "control": merge_section(state.get("control"), event="planner"),
        }

    return planner
