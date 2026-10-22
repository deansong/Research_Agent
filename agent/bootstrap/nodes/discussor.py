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


def make_discussor(backend, instructions: str = DISCUSSOR_INSTRUCTIONS,
                   checklist: str = "", research: bool = False):
    """`instructions` (the persona) and `checklist` (the experiment-design
    skill, agent/skills/experiment_design.md) are parameters so a session can
    replace them -- see agent/bootstrap/settings.py. `research`: whether it
    may ask the research agent before it asks you."""
    from agent import skills
    from agent.bootstrap.nodes.researcher import MAX_ROUNDS, report_text

    checklist = checklist or skills.experiment_design()

    def discussor(state: BootstrapState):
        print("\n[discussor] thinking about what you need...")

        providers = dict(state.get("providers", {}))
        role_threads = dict(providers.get("role_threads", {}))
        thread_id = role_threads.get("discussor")

        human = dict(state.get("human", {}))
        last_answer = human.get("last_answer", "").strip()
        found = dict(state.get("research") or {})
        fresh = bool(found.get("fresh")) and bool(found.get("reports"))
        may_research = research and not fresh and int(found.get("rounds", 0)) < MAX_ROUNDS
        availability = (
            "\n\nResearch is available: if a suggestion depends on the literature, "
            "put up to three short questions in `research` and ask the human "
            "nothing yet -- you will get the findings first."
            if may_research else
            "\n\nResearch is not available this turn: leave `research` empty.")

        if fresh:
            # Back from the research agent: the same conversation, now with
            # the findings, and this time the human IS asked.
            prompt = (
                f"The research agent answered your questions:\n\n"
                f"{report_text(found['reports'][-1])}\n\n"
                f"Now ask the human everything still open, all at once, with "
                f"suggestions grounded in these findings -- cite the source in "
                f"the suggestion. Update `requirements`."
                + availability
            )
        elif thread_id is None:
            prompt = (
                f"The human wants this done:\n\n{state['user_request']}\n\n"
                f"Begin discovery: ask EVERY question you "
                f"still need, all in this one turn, each with your suggested answer. "
                f"Fill `requirements` with their answers so far and your suggestions "
                f"marked (assumed). The human ends the discussion by typing /plan; "
                f"you only advise.\n\n{checklist}"
                + availability
            )
        else:
            prompt = (
                f"The human replied:\n\n{last_answer or '(nothing -- they came back to talk more)'}"
                f"\n\nAny question they did not answer keeps your suggested answer. "
                f"Update `requirements`. Ask only the follow-ups their answers opened, "
                f"all at once -- or none, and set advice to \"ready_to_plan\"."
                + availability
            )

        sent = activity.exchange(thread_id=thread_id,
                                 instructions=instructions,
                                 prompt=prompt)
        activity.arm_progress(backend, state.get('session_dir', ''), 'discussor',
                              sent=sent)
        run = backend.run_structured(
            thread_id=thread_id,
            repo_path=state["repo_path"],
            access=Access.READ_ONLY,
            developer_instructions=instructions,
            prompt=prompt,
            output_model=DiscussorOutput,
        )
        # Keep what the provider did, beside the work-phase nodes' records.
        # session_dir is already in BootstrapState, so no plumbing is needed --
        # see agent/activity.py for why this goes to disk and not to state.
        activity.disarm_progress(backend, state.get("session_dir", ""), "discussor")
        activity.write(state.get("session_dir", ""), "discussor", run.events, sent=sent,
                       summary=activity.scalars(run.data),
                       usage=usage_to_dict(run.usage) if run.usage else None,
                       backend=backend, thread_id=run.thread_id,
                       output=run.data.model_dump(mode="json", by_alias=True))
        activity.record_visit(state.get("session_dir", ""), "discussor", kind="design",
                              provider=getattr(backend, "name", ""),
                              model=getattr(backend, "model", "") or "",
                              thread=run.thread_id)

        role_threads["discussor"] = run.thread_id
        providers["role_threads"] = role_threads
        usage = record_usage(state.get("usage_by_role", {}), "discussor", run.usage)

        reply = run.data.reply.strip()
        questions = [q for q in run.data.questions if q.question.strip()]
        question = _ask(questions)
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

        wants = ([q.strip() for q in run.data.research if q.strip()][:3]
                 if may_research else [])
        if wants:
            # The graph goes to the researcher next; the human is asked after.
            question = ""
            print("[discussor] asking the research agent first: " + "; ".join(wants))
            spoken = (f"{reply}\n\n" if reply else "") + "Looking these up first:\n" + \
                "\n".join(f"- {q}" for q in wants)
        else:
            spoken = f"{reply}\n\n{question}".strip() if question else reply

        return {
            "research": merge_section(found, questions=wants, fresh=False),
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


def _ask(questions) -> str:
    """All the open questions as one message, each with its suggestion -- so
    the human can answer only the ones they disagree with."""
    if not questions:
        return ""
    lines = ["Answer any of these by number. Anything you leave out, my "
             "suggestion stands -- or type /plan to go ahead with all of them.", ""]
    for number, item in enumerate(questions, 1):
        lines.append(f"{number}. [{item.topic.strip()}] {item.question.strip()}")
        if item.suggestion.strip():
            lines.append(f"   Suggested: {item.suggestion.strip()}")
        if item.why.strip():
            lines.append(f"   Why it matters: {item.why.strip()}")
    return "\n".join(lines)
