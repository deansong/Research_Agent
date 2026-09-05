from __future__ import annotations

from openai_codex import Sandbox

from agent.codex_backend import CodexBackend
from agent.prompts import DISCUSSOR_INSTRUCTIONS
from agent.schemas import DiscussorOutput
from agent.state import AgentState, merge_section
from agent.telemetry import record_usage



def make_discussor(backend: CodexBackend):
    def discussor(state: AgentState):
        print("\n[discussor] understanding requirements...")

        codex_state = dict(state.get("codex", {}))
        role_threads = dict(codex_state.get("role_threads", {}))
        thread_id = role_threads.get("discussor")

        discussion = dict(state.get("discussion", {}))
        human_answer = discussion.get("last_human_answer", "").strip()

        if thread_id is None:
            prompt = f"""
We are beginning task #{state.get('task_cycle', 1)}.

The human's initial request is:

{state['user_request']}

Begin requirements discovery. If the request is already sufficiently clear,
mark it ready. Otherwise ask exactly one high-value question.
"""
        elif human_answer:
            prompt = f"""
The human answered your previous question:

{human_answer}

Continue requirements discovery. Ask one additional important question or
finalize the requirements if sufficiently clear.
"""
        else:
            prompt = f"""
A NEW high-level task has started in the same repository.

Task #{state.get('task_cycle', 1)}

Human request:

{state['user_request']}

Treat this as fresh requirements discovery while retaining useful repository
knowledge from earlier work. Ask one important question if necessary,
otherwise finalize requirements.
"""

        run = backend.run_structured(
            thread_id=thread_id,
            repo_path=state["repo_path"],
            sandbox=Sandbox.read_only,
            developer_instructions=DISCUSSOR_INSTRUCTIONS,
            prompt=prompt,
            output_model=DiscussorOutput,
        )

        role_threads["discussor"] = run.thread_id
        codex_state["role_threads"] = role_threads
        usage = record_usage(state.get("usage_by_role", {}), "discussor", run.usage)

        if run.data.ready:
            requirements = run.data.requirements.strip()
            print("\n[discussor] requirements ready:")
            print(requirements)

            return {
                "codex": codex_state,
                "usage_by_role": usage,
                "discussion": merge_section(
                    discussion,
                    requirements=requirements,
                    ready=True,
                    last_human_answer="",
                ),
                "human": merge_section(
                    state.get("human"),
                    question="",
                    context="",
                ),
            }

        question = run.data.question.strip() or (
            "What important behavior or constraint should I clarify before planning?"
        )
        print(f"\n[discussor] needs human input: {question}")

        return {
            "codex": codex_state,
            "usage_by_role": usage,
            "discussion": merge_section(
                discussion,
                requirements=run.data.requirements.strip(),
                ready=False,
                last_human_answer="",
            ),
            "human": merge_section(
                state.get("human"),
                question=question,
                context=run.data.requirements.strip(),
                purpose="discussion",
                return_to="discussor",
            ),
        }

    return discussor
