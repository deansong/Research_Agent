"""
WHAT:  Turns the discussion into a numbered, reviewable plan -- before any
       graph exists.
WHY:   The designer used to jump straight from a conversation to a topology.
       Nothing ever wrote down what the work actually CONSISTS of, so the
       designer had to infer both the steps and the shape in one leap, and it
       routinely produced one node carrying an entire project.
CONCEPT: A node that drives a human gate. It writes plan.json to disk and
       stops for approval; graph.py routes to `human`, not to the designer.

The plan is also what keeps each generated node's context small later: the
designer assigns step ids to nodes, and a node's prompt receives only its own
steps. See {my_steps} in agent/agentfolder/render.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent import activity
from agent.backends.base import Access, BackendOutputError
from agent.bootstrap.prompts import PLANNER_INSTRUCTIONS
from agent.bootstrap.schemas import PlannerOutput
from agent.bootstrap.state import BootstrapState, transcript_text
from agent.statelib import merge_section
from agent.telemetry import record_usage, usage_to_dict


def make_planner(backend, paths):
    def planner(state: BootstrapState):
        design = dict(state.get("design", {}))
        revision = design.get("plan_feedback", "").strip()

        print("\n[planner] working out the steps...")

        providers = dict(state.get("providers", {}))
        role_threads = dict(providers.get("role_threads", {}))
        thread_id = role_threads.get("planner")

        if revision:
            prompt = (
                f"Revise the plan. The human said:\n\n{revision}\n\n"
                f"Return the complete revised plan, not a patch."
            )
        else:
            prompt = (
                f"The human's request:\n\n{state['user_request']}\n\n"
                f"The conversation:\n\n{transcript_text(state)}\n\n"
                f"Requirements the discussor settled on:\n"
                f"{state.get('discussion', {}).get('requirements', '(none)')}\n\n"
                f"Inspect the repository, then break this into numbered steps."
            )

        try:
            run = backend.run_structured(
                thread_id=thread_id,
                repo_path=state["repo_path"],
                access=Access.READ_ONLY,
                developer_instructions=PLANNER_INSTRUCTIONS,
                prompt=prompt,
                output_model=PlannerOutput,
            )
            # Keep what the provider did, beside the work-phase nodes' records.
            # session_dir is already in BootstrapState, so no plumbing is needed --
            # see agent/activity.py for why this goes to disk and not to state.
            activity.write(state.get("session_dir", ""), "planner", run.events,
                           usage=usage_to_dict(run.usage) if run.usage else None)
        except BackendOutputError as exc:
            print(f"\n[planner] the model's reply did not fit the schema: {exc}")
            return {
                "design": merge_section(design, plan_feedback=f"Your reply was invalid: {exc}"),
            }

        role_threads["planner"] = run.thread_id
        providers["role_threads"] = role_threads
        usage = record_usage(state.get("usage_by_role", {}), "planner", run.usage)

        plan = run.data.model_dump(mode="json")

        # Written to disk BEFORE approval, on purpose: the whole point of the
        # gate is that you can open this file, reword a step, drop one, add a
        # substep -- and the approved plan is whatever the file says.
        Path(paths.plan).write_text(json.dumps(plan, indent=2) + "\n")

        print(f"\n[planner] {run.data.summary}\n")
        print(render_plan(plan))
        print(f"\n[planner] written to {paths.plan}")

        return {
            "providers": providers,
            "usage_by_role": usage,
            "design": merge_section(design, plan=plan, plan_feedback=""),
            "human": merge_section(
                state.get("human"),
                purpose="plan_review",
                question=(
                    "Approve this plan and design an agent for it?\n"
                    "  /approve            go ahead\n"
                    "  /revise <what>      have the planner change it\n"
                    "  /discuss            talk it over first\n"
                    "  /exit               stop here\n\n"
                    f"You can also edit {Path(paths.plan).name} by hand first -- "
                    f"/approve re-reads it from disk."
                ),
                context="",
                return_to="human",
            ),
        }

    return planner


def render_plan(plan: dict) -> str:
    """The plan as a person should read it."""
    lines = []
    for step in plan.get("steps", []):
        lines.append(f"  {step['id']}. {step['title']}")
        if step.get("detail"):
            lines.append(f"       {step['detail']}")
        for sub in step.get("substeps", []):
            lines.append(f"       {sub['id']} {sub['title']}")
    return "\n".join(lines)


def outline(plan: dict) -> str:
    """One line per top-level step -- what OTHER nodes are handling."""
    return "\n".join(f"  {s['id']}. {s['title']}" for s in (plan or {}).get("steps", []))


def steps_for(plan: dict, ids: list[str]) -> str:
    """Just the steps a particular node owns, rendered in full.

    This is the context-control mechanism: a node sees its own steps in detail
    and everyone else's as a single line each.
    """
    if not ids:
        return ""
    wanted = set(ids)
    lines = []
    for step in (plan or {}).get("steps", []):
        subs = [s for s in step.get("substeps", []) if s["id"] in wanted]
        if step["id"] in wanted:
            lines.append(f"{step['id']}. {step['title']}")
            if step.get("detail"):
                lines.append(f"   {step['detail']}")
            subs = step.get("substeps", [])          # own the whole step
        elif not subs:
            continue
        for sub in subs:
            lines.append(f"   {sub['id']} {sub['title']}")
            if sub.get("detail"):
                lines.append(f"      {sub['detail']}")
    return "\n".join(lines)
