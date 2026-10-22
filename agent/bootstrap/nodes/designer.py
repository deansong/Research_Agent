"""
WHAT:  Designs the agent: the whole graph, and a short spec per node, starting
       from a working design that code built from the plan.
WHY:   This is the node the entire feature exists for.
CONCEPT: A node whose OUTPUT is another graph. Note it produces data only; the
       writer puts it on disk and the validator checks it. Keeping generation,
       materialisation and checking in three nodes is what makes each one
       simple enough to read.

--------------------------------------------------------------------------
WHY IT EDITS INSTEAD OF WRITING
--------------------------------------------------------------------------
It used to write the whole folder from a blank page in one answer. On this
repo's own sessions that failed on bookkeeping, every time, with the same
symptoms: "graph.json lists 30 nodes and nodes.json has 1", edges to nodes it
never wrote, entries missing required fields -- and each repair rewrote all
of it and lost something else.

So now:
  1. code builds a STARTING DESIGN from the approved plan (blueprint.py) and
     checks that it compiles to a folder that passes the validator;
  2. the model returns a design in the same format: the whole graph.json --
     any topology, loops and routers included -- and a few lines per node:
     role, steps, what it reads, instructions. spec.py writes the prompts,
     output fields and branch enums from that. Measured: asked for ~30 full
     node entries a model sends one or none; asked for ~30 specs, all of them;
  3. a repair shows the design as it stands, which is a few KB;
  4. if the attempts run out, the validator falls back to the last agent that
     passed. A design never ends worse than the starting one.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import types

from agent import activity
from agent.backends.base import (Access, BackendBusy, BackendOutputError,
                                 BackendTimeout)
from agent.bootstrap import blueprint
from agent.bootstrap.prompts import DESIGNER_INSTRUCTIONS, REPAIR_PREFIX
from agent.bootstrap.schemas import DesignerSpec
from agent.bootstrap.spec import SpecError, compile_design
from agent.bootstrap.state import BootstrapState, transcript_text
from agent.statelib import merge_section
from agent.telemetry import record_usage, usage_to_dict


def make_designer(backend, instructions: str = DESIGNER_INSTRUCTIONS,
                  reference: str | None = None,
                  repair_prefix: str = REPAIR_PREFIX,
                  topology: str = "hub", skills: str = ""):
    # All three are parameters so a session can replace them -- see
    # agent/bootstrap/settings.py, which also owns the default reference.
    # `topology` is which starting design it edits: "hub" or "pipeline".
    if reference is None:
        from agent.bootstrap.settings import designer_reference

        reference = designer_reference(topology)

    def designer(state: BootstrapState):
        design = dict(state.get("design", {}))
        attempt = int(design.get("attempt", 0))
        problems = design.get("problems", "").strip()
        plan = design.get("plan") or {}

        print(f"\n[designer] designing an agent (attempt {attempt + 1})...")

        providers = dict(state.get("providers", {}))
        role_threads = dict(providers.get("role_threads", {}))
        thread_id = role_threads.get("designer")

        if problems:
            # Fix the design as it stands -- the rejected attempt. `valid` is
            # kept unchanged for the validator's fallback.
            valid = design.get("valid") or {}
            current = design.get("spec") or {}
            prompt = (f"{repair_prefix}\n{problems}\n\n"
                      + _shown("THE DESIGN AS IT STANDS", current))
        else:
            # A fresh design starts from the CURRENT plan: a starting design
            # kept from an earlier one may belong to an older plan.
            current, valid = _starting_design(state, plan, topology)
            if valid:
                print(f"[designer] starting design built from the plan: "
                      f"{len(valid['graph']['nodes'])} nodes, valid")
            review = str(design.get("review_feedback") or "").strip()
            last_ran = design.get("valid_spec") or {}
            # An agent of the other shape -- a pipeline that ran before hubs,
            # say -- is not a starting point for this one.
            same_shape = (any(n.get("role") == "orchestrator"
                              for n in last_ran.get("nodes") or [])
                          == (topology != "pipeline"))
            if review and last_ran and same_shape:
                # A redesign after a supervisor's review: start from the
                # design that last ran, if it still fits the plan -- the
                # findings are about THAT agent, not a fresh one.
                try:
                    valid = compile_design(design["valid_spec"], plan)
                    current = design["valid_spec"]
                    print("[designer] redesigning from the agent that last ran")
                except SpecError:
                    pass
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
                # THE PLAN, which was once simply not here -- on a real
                # 13-step session 0 of the 13 step titles appeared in this
                # prompt, so every `steps: [...]` named ids never read.
                f"THE APPROVED PLAN. Every step id below must be assigned to "
                f"exactly one node's `steps`:\n\n{_plan_text(state)}\n\n"
                # NOT "inspect the repository, then design the agent": a
                # measured run spent its first minutes auditing git history
                # it does not need to lay out a graph. The plan is the input.
                + (f"FINDINGS FROM THE REVIEW OF THE LAST RUN -- act on every "
                   f"one that concerns the agent:\n{review}\n\n" if review else "")
                + f"Design the agent that carries out that plan, starting from "
                f"the design at the end of this message."
                # All of this rides in the PROMPT, not the instructions:
                # over ~6 KB of developer_instructions a Codex turn never
                # completes. See agent/bootstrap/prompts.py.
                + reference
                + _skills_catalog()
                + (f"\n\n{skills}" if skills else "")
                + "\n\n"
                # Last: it is what the answer is an edit of.
                + _shown("THE STARTING DESIGN -- built from the plan; it compiles "
                         "to an agent that passes every check", current)
            )

        # The design `valid` was compiled from: on a repair, whatever passed
        # last; on a fresh design, the starting point just chosen.
        valid_spec = (design.get("valid_spec") if problems else current) or {}

        try:
            sent = activity.exchange(thread_id=thread_id,
                                     instructions=instructions,
                                     prompt=prompt)
            activity.arm_progress(backend, state.get('session_dir', ''), 'designer',
                                  sent=sent)
            try:
                run = backend.run_structured(
                    thread_id=thread_id,
                    repo_path=state["repo_path"],
                    access=Access.READ_ONLY,
                    developer_instructions=instructions,
                    prompt=prompt,
                    output_model=DesignerSpec,
                )
            finally:
                activity.disarm_progress(backend, state.get("session_dir", ""), "designer")
            # Keep what the provider did, beside the work-phase nodes' records.
            # session_dir is already in BootstrapState, so no plumbing is needed --
            # see agent/activity.py for why this goes to disk and not to state.
            activity.write(state.get("session_dir", ""), "designer", run.events, sent=sent,
                           summary=activity.scalars(run.data),
                           usage=usage_to_dict(run.usage) if run.usage else None,
                           backend=backend, thread_id=run.thread_id,
                           output=run.data.model_dump(mode="json", by_alias=True))
            activity.record_visit(state.get("session_dir", ""), "designer", kind="design",
                                  provider=getattr(backend, "name", ""),
                                  model=getattr(backend, "model", "") or "",
                                  thread=run.thread_id)
        except (BackendOutputError, BackendTimeout, BackendBusy) as exc:
            # A failed turn is a failed ATTEMPT, not a failed session: same
            # repair path as a structurally bad design, and when the attempts
            # run out the validator falls back to `valid`.
            print(f"\n[designer] the turn failed: {type(exc).__name__}: {exc}")
            return {
                "design": merge_section(
                    design,
                    proposal={},
                    spec=current,
                    valid=valid,
                    valid_spec=valid_spec,
                    attempt=attempt + 1,
                    problems=(f"Your reply could not be used "
                              f"({type(exc).__name__}):\n{exc}"),
                ),
            }

        role_threads["designer"] = run.thread_id
        providers["role_threads"] = role_threads
        usage = record_usage(state.get("usage_by_role", {}), "designer", run.usage)

        answered = run.data.model_dump(mode="json", by_alias=True)
        print(f"\n[designer] agent {run.data.graph.name!r}: "
              f"{len(run.data.graph.nodes)} nodes, {len(run.data.nodes)} specs")
        if run.data.rationale.strip():
            print(f"[designer] {run.data.rationale.strip()}")
        try:
            proposal = compile_design(run.data, plan)
        except SpecError as exc:
            listed = "\n".join(f"- {line}" for line in exc.problems)
            print(f"[designer] the design does not compile:\n{listed}")
            return {
                "providers": providers,
                "usage_by_role": usage,
                "design": merge_section(
                    design, proposal={}, spec=answered, valid=valid,
                    valid_spec=valid_spec, attempt=attempt + 1,
                    problems=f"The design does not compile:\n{listed}"),
            }

        issues = list(dict.fromkeys(blueprint.plan_issues(plan)
                                    + [i.strip() for i in run.data.plan_issues if i.strip()]))
        return {
            "providers": providers,
            "usage_by_role": usage,
            "design": merge_section(
                design,
                plan_issues=issues,
                spec=answered,
                valid_spec=valid_spec,
                # Plain dicts, not the model: a Pydantic instance in a
                # loosely-typed state field reloads as a plain dict with only a
                # line on stderr. Store data, re-validate on read.
                proposal=proposal,
                valid=valid,
                task_brief=proposal["task_brief"],
                rationale=proposal["rationale"],
                attempt=attempt + 1,
                problems="",
            ),
        }

    return designer


# ---------------------------------------------------------------------------
# the starting agent, and applying an edit to it
# ---------------------------------------------------------------------------

def _starting_design(state, plan: dict, topology: str = "hub") -> tuple[dict, dict]:
    """(the starting design, the folder it compiles to), or ({}, {}).

    The folder is checked through the REAL writer and validator before it is
    used, so "valid" is a fact rather than a property blueprint.py promises.
    ({}, {}) means the model designs from a blank page, as it always used to.
    """
    from agent.bootstrap.nodes.validator import _check
    from agent.bootstrap.nodes.writer import make_writer

    if not plan.get("steps"):
        return {}, {}
    try:
        start = blueprint.starting_design(
            plan,
            request=str(state.get("user_request", "")),
            requirements=str((state.get("discussion") or {}).get("requirements", "")),
            topology=topology,
        )
        proposal = compile_design(start, plan)
    except (blueprint.BlueprintError, SpecError) as exc:
        print(f"[designer] could not build a starting design from the plan: {exc}")
        return {}, {}

    with tempfile.TemporaryDirectory() as tmp:
        paths = types.SimpleNamespace(staging=f"{tmp}/agent.tmp", brief=f"{tmp}/brief.md")
        make_writer(paths)({"design": {"proposal": proposal}})
        problems = _check(pathlib.Path(paths.staging), plan)
    if problems:
        print(f"[designer] the starting design did not validate, so starting "
              f"from a blank page:\n{problems}")
        return {}, {}
    return start, proposal


def _skills_catalog() -> str:
    """The skills a designed node can be given, for the designer to suggest
    per node. Each node's starting choice is skills/suggestions.json's."""
    from agent import skills

    available = skills.discover()
    if not available:
        return ""
    lines = "\n".join(f"  - {s.name}: {s.description}" for s in available)
    return ("\n\n" + "-" * 74 + "\nSKILLS THE NODES CAN BE GIVEN\n" + "-" * 74 + "\n"
            "Shared instructions and tools in skills/. Per node, `skill_mode` is "
            "none, auto (the node is shown this list and reads what it needs) or "
            "selected (the skills named in `skills`, given in full). The starting "
            "design has the defaults; change a node's choice where a skill fits "
            "its job, or does not.\n" + lines)


def _shown(title: str, design: dict) -> str:
    """A design as the model answers it: the same shape, so it can be edited."""
    rule = "-" * 74
    if not design:
        return (f"{rule}\n{title}\n{rule}\n(none -- there is no plan to build "
                f"one from, so lay the whole agent out yourself.)")
    # No task_brief: the starting one only restates the conversation above,
    # and the designer writes its own.
    body = json.dumps({"graph": design["graph"], "nodes": design["nodes"]}, indent=1)
    return (f"{rule}\n{title}\n{rule}\nReturn the WHOLE design in this shape -- "
            f"every node in graph.nodes with exactly one spec in `nodes`.\n\n{body}")


#: How much of the conversation to send. The plan is bounded -- four stages,
#: eight substeps each --
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
