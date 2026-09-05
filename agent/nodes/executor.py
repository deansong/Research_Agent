from __future__ import annotations

from agent.backends.base import Access

from agent.git_utils import snapshot
from agent.prompts import EXECUTOR_INSTRUCTIONS
from agent.schemas import ExecutorOutput
from agent.state import AgentState, merge_section
from agent.telemetry import record_usage



def make_executor(backend):
    def executor(state: AgentState):
        execution = dict(state.get("execution", {}))
        planning = dict(state.get("planning", {}))
        providers = dict(state.get("providers", {}))

        workstream = execution.get("workstream", "main").strip() or "main"
        print(f"\n[executor:{workstream}] coding...")

        executor_threads = dict(providers.get("executor_threads", {}))
        seen_generations = dict(providers.get("executor_seen_plan_generation", {}))

        thread_id = executor_threads.get(workstream)
        current_generation = int(planning.get("generation", 0))
        last_seen_generation = int(seen_generations.get(workstream, -1))

        if thread_id is None or last_seen_generation != current_generation:
            prompt = f"""
You are beginning work under a new or revised implementation plan.

Workstream:
{workstream}

Overall plan:
{planning.get('plan', '')}

Your CURRENT task is:
{execution.get('current_task', '')}

Implement this task in the repository. Inspect existing code before changing it,
run appropriate verification, and avoid unrelated future plan items unless they
are required to make the current task correct.
"""
            seen_generations[workstream] = current_generation
        else:
            prompt = f"""
Next task from the orchestrator:

{execution.get('current_task', '')}

Continue using your existing repository and workstream context. Implement it and
run appropriate verification.
"""

        run = backend.run_structured(
            thread_id=thread_id,
            repo_path=state["repo_path"],
            access=Access.WRITE,
            developer_instructions=EXECUTOR_INSTRUCTIONS,
            prompt=prompt,
            output_model=ExecutorOutput,
        )

        executor_threads[workstream] = run.thread_id
        providers["executor_threads"] = executor_threads
        providers["executor_seen_plan_generation"] = seen_generations

        role = f"executor:{workstream}"
        usage = record_usage(state.get("usage_by_role", {}), role, run.usage)
        git = snapshot(state["repo_path"])

        print("\n[executor] summary:")
        print(run.data.summary)
        if run.data.tests_run:
            print("\n[executor] tests:")
            for command in run.data.tests_run:
                print("  ", command)
        if git.diff_stat:
            print("\n[executor] diff:")
            print(git.diff_stat)

        return {
            "providers": providers,
            "usage_by_role": usage,
            "execution": merge_section(
                execution,
                result=run.data.model_dump(),
                git_status=git.status,
                git_diff_stat=git.diff_stat,
            ),
            "control": merge_section(state.get("control"), event="executor"),
        }

    return executor
