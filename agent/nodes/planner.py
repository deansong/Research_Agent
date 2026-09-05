"""
WHAT:  The planner node -- turns the discussion into a technical plan.
WHY:   Separating "what do we want" (discussor) from "how do we build it"
       (planner) keeps each model turn focused and each prompt short.
CONCEPT: Reading a reducer-backed channel.  The planner's whole input is the
       `transcript` list the discussor and human have been appending to.

Reached ONLY from nodes/human.py via /plan or /replan -- never automatically.
"""

from __future__ import annotations

from openai_codex import Sandbox

from agent.prompts import PLANNER_INSTRUCTIONS
from agent.schemas import PlannerOutput
from agent.state import AgentState, merge_section, transcript_for_cycle
from agent.telemetry import record_usage


def make_planner(backend):
    def planner(state: AgentState):
        print("\n[planner] turning the discussion into a plan...")

        # ---- step 1: read state ---------------------------------------------
        providers = dict(state.get("providers", {}))
        role_threads = dict(providers.get("role_threads", {}))
        thread_id = role_threads.get("planner")

        planning = dict(state.get("planning", {}))
        feedback = planning.get("feedback", "").strip()
        requirements = state.get("discussion", {}).get("requirements", "")
        cycle = int(state.get("task_cycle", 1))

        # The whole point of requirement 2: plan from what was actually said,
        # both sides of it, not just the discussor's summary.
        conversation = transcript_for_cycle(state, cycle)

        # ---- step 2: build the prompt ---------------------------------------
        if planning.get("plan") and feedback:
            # We already have a plan and someone (the orchestrator, or the
            # human via /replan) wants it revised.
            prompt = f"""
Revise your current implementation plan.

Feedback:

{feedback}

For reference, the full conversation with the human was:

{conversation}

Inspect the repository again if necessary. Return the revised plan, the
workstream, and the next executable task.
"""
        else:
            prompt = f"""
The human has finished discussing and asked for a plan.

Task #{cycle}

Their original request:

{state['user_request']}

The full conversation with the human:

{conversation}

The requirements brief the discussor maintained:

{requirements or '(none recorded)'}
{f'''
Extra guidance the human gave when asking for the plan:

{feedback}
''' if feedback else ''}
Inspect the repository and create an implementation plan. Return the plan, the
most appropriate workstream slug, and the first concrete executor task.
"""

        # ---- step 3: call the model -----------------------------------------
        run = backend.run_structured(
            thread_id=thread_id,
            repo_path=state["repo_path"],
            sandbox=Sandbox.read_only,
            developer_instructions=PLANNER_INSTRUCTIONS,
            prompt=prompt,
            output_model=PlannerOutput,
        )

        # ---- step 4: record handles and cost --------------------------------
        role_threads["planner"] = run.thread_id
        providers["role_threads"] = role_threads
        usage = record_usage(state.get("usage_by_role", {}), "planner", run.usage)

        # generation is how executors detect that their plan went stale.
        generation = int(planning.get("generation", 0)) + 1

        print("\n[planner] plan:")
        print(run.data.plan)
        print("\n[planner] first task:", run.data.next_task)

        # ---- step 5: return the update --------------------------------------
        return {
            "providers": providers,
            "usage_by_role": usage,
            "planning": merge_section(
                planning,
                plan=run.data.plan.strip(),
                generation=generation,
                # Clear the feedback so the next planner run does not think it
                # is still being asked to revise.
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
