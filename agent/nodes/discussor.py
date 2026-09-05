"""
WHAT:  The discussor node -- multi-turn requirement discovery with the human.
WHY:   Jumping straight from a one-line request to a plan produces bad plans.
       This node asks one good question at a time and keeps a running brief.
CONCEPT: A node is a plain function (state) -> partial state update.  This one
       also shows the reducer pattern: it returns ONLY its new transcript
       entry, never the whole list.

Requirement 2 in one sentence: this node ALWAYS hands control to the human
afterwards (see the plain edge in graph.py).  It cannot advance to planning
on its own -- it can only advise.
"""

from __future__ import annotations

from openai_codex import Sandbox

from agent.prompts import DISCUSSOR_INSTRUCTIONS
from agent.schemas import DiscussorOutput
from agent.state import AgentState, merge_section
from agent.telemetry import record_usage


def make_discussor(backend):
    """Return the node function, with `backend` captured in the closure.

    Why a factory instead of a plain function: LangGraph calls nodes with just
    the state, so there is no parameter to pass the backend through.  Closing
    over it keeps the dependency explicit and testable (pass a fake backend).
    """

    def discussor(state: AgentState):
        print("\n[discussor] thinking about your requirements...")

        # ---- step 1: read what we need out of state ------------------------
        providers = dict(state.get("providers", {}))
        role_threads = dict(providers.get("role_threads", {}))
        thread_id = role_threads.get("discussor")

        discussion = dict(state.get("discussion", {}))
        human = dict(state.get("human", {}))
        last_answer = human.get("last_answer", "").strip()
        cycle = int(state.get("task_cycle", 1))

        # ---- step 2: build the prompt for this turn ------------------------
        if thread_id is None:
            # No provider conversation yet: this is the very first turn.
            prompt = f"""
We are beginning task #{cycle}.

The human's initial request is:

{state['user_request']}

Begin requirements discovery. Ask exactly one high-value question, or say what
you understand so far. Remember that the human decides when to stop discussing
by typing /plan -- you only advise.
"""
        elif last_answer:
            prompt = f"""
The human replied:

{last_answer}

Continue requirements discovery. Ask one more important question if there is a
genuinely useful one, otherwise summarise where things stand and set advice to
"ready_to_plan".
"""
        else:
            # A thread exists but there is no fresh answer: either a new task
            # cycle, or the human came back with /discuss.
            prompt = f"""
A NEW high-level task has started in the same repository.

Task #{cycle}

Human request:

{state['user_request']}

Treat this as fresh requirements discovery while keeping useful repository
knowledge from earlier work.
"""

        # ---- step 3: call the model ----------------------------------------
        run = backend.run_structured(
            thread_id=thread_id,
            repo_path=state["repo_path"],
            sandbox=Sandbox.read_only,
            developer_instructions=DISCUSSOR_INSTRUCTIONS,
            prompt=prompt,
            output_model=DiscussorOutput,
        )

        # ---- step 4: remember the conversation handle and the token cost ---
        role_threads["discussor"] = run.thread_id
        providers["role_threads"] = role_threads
        usage = record_usage(state.get("usage_by_role", {}), "discussor", run.usage)

        # ---- step 5: show the human what was said --------------------------
        reply = run.data.reply.strip()
        question = run.data.question.strip()
        requirements = run.data.requirements.strip()

        if reply:
            print(f"\n[discussor] {reply}")

        # The advisory hint.  Note it is PRINTED, not routed on -- the graph
        # goes to `human` either way.  This is requirement 2: the model keeps
        # its opinion, you keep the decision.
        if run.data.advice == "ready_to_plan":
            reason = run.data.advice_reason.strip()
            print(
                "\n[discussor] hint: I think we have enough to plan"
                + (f" ({reason})" if reason else "")
                + " -- type /plan when you are ready."
            )

        # ---- step 6: return the state update -------------------------------
        # The transcript entry records BOTH the reply and the question, so the
        # planner later sees the discussor's reasoning and not just its asks.
        spoken = reply
        if question:
            spoken = f"{spoken}\n\nQuestion: {question}" if spoken else f"Question: {question}"

        return {
            # ONLY the new entry.  The operator.add reducer appends it.
            "transcript": [{"role": "discussor", "text": spoken, "cycle": cycle}],
            "providers": providers,
            "usage_by_role": usage,
            "discussion": merge_section(
                discussion,
                requirements=requirements,
                advice=run.data.advice,
                advice_reason=run.data.advice_reason.strip(),
            ),
            "human": merge_section(
                human,
                question=question or "Anything to add? Type /plan when you want a plan.",
                context=requirements,
                purpose="discussion",
                return_to="discussor",
                last_answer="",
            ),
        }

    return discussor
