"""
WHAT:  Designs the agent -- emits a whole folder as structured data.
WHY:   This is the node the entire feature exists for.
CONCEPT: A node whose OUTPUT is another graph. Note it produces data only; the
       writer puts it on disk and the validator checks it. Keeping generation,
       materialisation and checking in three nodes is what makes each one
       simple enough to read.
"""

from __future__ import annotations

from agent import activity
from agent.backends.base import Access, BackendOutputError
from agent.bootstrap.prompts import (CONTROL_FLOW, DESIGNER_INSTRUCTIONS,
                                     PROMPT_REFERENCE, REPAIR_PREFIX,
                                     VERIFIER_LOOP, research_skeleton)
from agent.bootstrap.schemas import DesignerOutput
from agent.bootstrap.state import BootstrapState, transcript_text
from agent.statelib import merge_section
from agent.telemetry import record_usage, usage_to_dict


def make_designer(backend):
    def designer(state: BootstrapState):
        design = dict(state.get("design", {}))
        attempt = int(design.get("attempt", 0))
        problems = design.get("problems", "").strip()

        print(f"\n[designer] designing an agent (attempt {attempt + 1})...")

        providers = dict(state.get("providers", {}))
        role_threads = dict(providers.get("role_threads", {}))
        thread_id = role_threads.get("designer")

        if problems:
            prompt = f"{REPAIR_PREFIX}\n{problems}"
        else:
            prompt = (
                # NOT state["user_request"] as well: it is byte-identical to
                # the transcript's first entry, so sending both shipped the
                # same 6.5 KB twice on a measured run.
                f"The conversation with the human:\n\n"
                f"{_conversation(state)}\n\n"
                f"Requirements the discussor settled on:\n"
                f"{state.get('discussion', {}).get('requirements', '(none)')}\n\n"
                f"Extra guidance given with /plan:\n"
                f"{state.get('human', {}).get('last_answer', '') or '(none)'}\n\n"
                # THE PLAN, which was simply not here. The line below still
                # said "design the agent from the plan above", and on a real
                # 13-step session 0 of the 13 step titles appeared anywhere in
                # this prompt -- so the designer invented its own stages and
                # every `steps: [...]` it wrote named ids it had never read.
                f"THE APPROVED PLAN. Every step id below must be assigned to "
                f"exactly one node's `steps`:\n\n{_plan_text(state)}\n\n"
                # NOT "inspect the repository, then design the agent", which
                # is what this said and is what a measured run spent its first
                # minutes doing -- `git show` of a deletion commit, auditing a
                # tree it does not need to read to lay out a graph. The plan is
                # the input. DESIGNER_INSTRUCTIONS says so too; an instruction
                # contradicted by the prompt is not an instruction.
                f"Design the agent that carries out that plan. Return the "
                f"task_brief and the complete folder."
                # All of this rides in the PROMPT, not the instructions:
                # over ~6 KB of developer_instructions a Codex turn never
                # completes. See agent/bootstrap/prompts.py.
                + VERIFIER_LOOP
                + PROMPT_REFERENCE
                + CONTROL_FLOW
                # Last, deliberately. It is the most specific and most
                # actionable thing the designer is shown, and the end of a
                # prompt is where that belongs.
                + research_skeleton()
            )

        try:
            activity.arm_progress(backend, state.get('session_dir', ''), 'designer')
            run = backend.run_structured(
                thread_id=thread_id,
                repo_path=state["repo_path"],
                access=Access.READ_ONLY,
                developer_instructions=DESIGNER_INSTRUCTIONS,
                prompt=prompt,
                output_model=DesignerOutput,
            )
            # Keep what the provider did, beside the work-phase nodes' records.
            # session_dir is already in BootstrapState, so no plumbing is needed --
            # see agent/activity.py for why this goes to disk and not to state.
            activity.disarm_progress(backend, state.get("session_dir", ""), "designer")
            activity.write(state.get("session_dir", ""), "designer", run.events,
                           usage=usage_to_dict(run.usage) if run.usage else None)
        except BackendOutputError as exc:
            # The model produced something that is not even a valid
            # DesignerOutput. Same repair path as a structurally bad design:
            # one counter, three failure sources, one message format.
            print(f"\n[designer] the model's reply did not fit the schema: {exc}")
            return {
                "design": merge_section(
                    design,
                    proposal={},
                    attempt=attempt + 1,
                    problems=f"Your reply was not valid JSON for the required schema:\n{exc}",
                ),
            }

        role_threads["designer"] = run.thread_id
        providers["role_threads"] = role_threads
        usage = record_usage(state.get("usage_by_role", {}), "designer", run.usage)

        data = run.data
        print(f"\n[designer] proposed agent {data.graph.name!r} with "
              f"{len(data.graph.nodes)} nodes: "
              f"{', '.join(n.name for n in data.graph.nodes)}")
        if data.rationale.strip():
            print(f"[designer] {data.rationale.strip()}")

        return {
            "providers": providers,
            "usage_by_role": usage,
            "design": merge_section(
                design,
                # model_dump(), not the object: a Pydantic instance in a
                # loosely-typed state field reloads as a plain dict with only a
                # line on stderr. Store data, re-validate on read.
                proposal=data.model_dump(mode="json", by_alias=True),
                task_brief=data.task_brief.strip(),
                rationale=data.rationale.strip(),
                attempt=attempt + 1,
                problems="",
            ),
        }

    return designer


#: How much of the conversation to send. The plan is bounded -- 20 steps --
#: but a discussion is not, and it is the one input that grows with every
#: turn somebody takes. Newest entries kept: the plan already carries what
#: the early conversation decided.
TRANSCRIPT_BUDGET = 12_000


def _conversation(state) -> str:
    """The transcript, newest-first-priority, within a budget.

    Unbounded until now, and it is the only unbounded thing in this prompt:
    every discussion turn adds to it for the rest of the session. A long
    interview would eventually make the design prompt mostly small talk.
    """
    entries = list(state.get("transcript", []))
    kept: list[dict] = []
    total = 0

    for entry in reversed(entries):
        text = str(entry.get("text", ""))
        if total + len(text) > TRANSCRIPT_BUDGET and kept:
            break
        kept.append(entry)
        total += len(text)

    kept.reverse()
    dropped = len(entries) - len(kept)
    body = transcript_text({"transcript": kept})
    if dropped:
        # Said, not silent. A prompt that has quietly lost context is worse
        # than a shorter one, because nothing downstream can tell.
        body = (f"[the {dropped} earliest message(s) are omitted for length; "
                f"the approved plan below is what they produced]\n\n" + body)
    return body


def _plan_text(state) -> str:
    """The approved plan, every step and substep, as the designer needs it.

    Rendered rather than dumped as JSON: it needs the ids (to assign them to
    nodes), the titles, the detail, and each step's `check` and `gate` --
    which is exactly what steps_for() produces for a node that owns
    everything. Half the size of the JSON, and the same information.
    """
    from agent.bootstrap.nodes.planner import steps_for

    plan = (state.get("design") or {}).get("plan") or {}
    steps = plan.get("steps") or []
    if not steps:
        return ("(no plan -- design from the conversation above, and say so "
                "in `rationale`)")
    ids = [step["id"] for step in steps]
    ids += [sub["id"] for step in steps for sub in step.get("substeps", [])]

    summary = str(plan.get("summary", "")).strip()
    head = f"{summary}\n\n" if summary else ""
    return head + steps_for(plan, ids)
