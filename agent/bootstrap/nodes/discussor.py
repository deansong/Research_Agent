"""
WHAT:  Works out what the human wants -- both the job and the shape of agent
       that should do it.
WHY:   Ported almost unchanged from the original agent's discussor. The only
       real difference is the prompt, which now also asks about shape.
CONCEPT: A node is a function (state) -> partial update. Note it has a single
       plain edge to `human`, so no model output can start the design. Only
       the human typing /plan can. Enforcement by topology, same as before.
"""

from __future__ import annotations

from agent import activity
from agent.backends.base import Access
from agent.bootstrap.prompts import DISCUSSOR_INSTRUCTIONS
from agent.bootstrap.schemas import DiscussorOutput
from agent.bootstrap.state import BootstrapState
from agent.statelib import merge_section
from agent.telemetry import record_usage, usage_to_dict


def make_discussor(backend):
    def discussor(state: BootstrapState):
        print("\n[discussor] thinking about what you need...")

        providers = dict(state.get("providers", {}))
        role_threads = dict(providers.get("role_threads", {}))
        thread_id = role_threads.get("discussor")

        human = dict(state.get("human", {}))
        last_answer = human.get("last_answer", "").strip()

        if thread_id is None:
            prompt = (
                f"The human wants this done:\n\n{state['user_request']}\n\n"
                f"Begin discovery. Ask exactly one high-value question about either the work "
                f"itself or the shape of agent that should do it. The human ends the "
                f"discussion by typing /plan; you only advise."
            )
        else:
            prompt = (
                f"The human replied:\n\n{last_answer or '(nothing -- they came back to talk more)'}"
                f"\n\nContinue. Ask one more genuinely useful question, or summarise where "
                f'things stand and set advice to "ready_to_plan".'
            )

        activity.arm_progress(backend, state.get('session_dir', ''), 'discussor')
        run = backend.run_structured(
            thread_id=thread_id,
            repo_path=state["repo_path"],
            access=Access.READ_ONLY,
            developer_instructions=DISCUSSOR_INSTRUCTIONS,
            prompt=prompt,
            output_model=DiscussorOutput,
        )
        # Keep what the provider did, beside the work-phase nodes' records.
        # session_dir is already in BootstrapState, so no plumbing is needed --
        # see agent/activity.py for why this goes to disk and not to state.
        activity.disarm_progress(backend, state.get("session_dir", ""), "discussor")
        activity.write(state.get("session_dir", ""), "discussor", run.events,
                       usage=usage_to_dict(run.usage) if run.usage else None)

        role_threads["discussor"] = run.thread_id
        providers["role_threads"] = role_threads
        usage = record_usage(state.get("usage_by_role", {}), "discussor", run.usage)

        reply = run.data.reply.strip()
        question = run.data.question.strip()
        requirements = run.data.requirements.strip()

        if reply:
            print(f"\n[discussor] {reply}")
        if run.data.advice == "ready_to_plan":
            reason = run.data.advice_reason.strip()
            print(
                "\n[discussor] hint: I think we have enough to design an agent"
                + (f" ({reason})" if reason else "")
                + " -- type /plan when you are ready."
            )

        spoken = reply
        if question:
            spoken = f"{spoken}\n\nQuestion: {question}" if spoken else f"Question: {question}"

        return {
            # Only the NEW entry -- the operator.add reducer appends it.
            "transcript": [{"role": "discussor", "text": spoken}],
            "providers": providers,
            "usage_by_role": usage,
            "discussion": merge_section(
                state.get("discussion"),
                requirements=requirements,
                advice=run.data.advice,
                advice_reason=run.data.advice_reason.strip(),
            ),
            "human": merge_section(
                human,
                question=question or "Anything to add? Type /plan to design the agent.",
                context=requirements,
                purpose="discussion",
                return_to="discussor",
                last_answer="",
            ),
        }

    return discussor
