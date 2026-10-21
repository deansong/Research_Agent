"""
WHAT:  Checks the written folder, and on success promotes agent.tmp/ to agent/.
WHY:   Validating from DISK rather than from the in-memory proposal proves the
       folder round-trips through JSON -- which catches things the in-memory
       object cannot show, like a duplicate key collapsing in nodes.json.
CONCEPT: A node with no model in it, that drives a conditional edge. It writes
       control decisions into state; graph.py's router only reads them.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from agent.agentfolder.load import AgentFolderError, load_agent_folder
from agent.agentfolder.validate import format_problems, validate_folder
from agent.bootstrap.state import BootstrapState
from agent.statelib import merge_section


def make_validator(paths, *, max_attempts: int):
    def validator(state: BootstrapState):
        design = dict(state.get("design", {}))
        attempt = int(design.get("attempt", 0))
        staging = Path(design.get("written_to") or paths.staging)

        problems_text = design.get("problems", "").strip()

        if not problems_text:
            # state["design"]["plan"], NOT state["plan"] -- the plan lives
            # inside the design section (planner.py writes it there, human.py
            # re-reads it from disk into the same place).
            problems_text = _check(
                staging, (state.get("design") or {}).get("plan") or {})

        if not problems_text:
            # This design passed: it is now the one a fallback or a
            # supervisor's redesign starts from.
            design = merge_section(design, valid=design.get("proposal") or design.get("valid"),
                                   valid_spec=design.get("spec") or design.get("valid_spec"))
            return _accept(state, design, staging)

        print(f"\n[validator] the design has problems:\n{problems_text}")

        if attempt < max_attempts:
            print(f"[validator] asking the designer to fix them "
                  f"(attempt {attempt + 1} of {max_attempts})")
            return {"design": merge_section(design, problems=problems_text)}

        print(f"[validator] gave up after {max_attempts} attempts.")

        # Never end worse than the start: the designer began from an agent
        # that passed (see nodes/designer.py). Offer that, with what went
        # wrong, rather than nothing.
        valid = design.get("valid") or {}
        if valid:
            from agent.bootstrap.nodes.writer import make_writer

            make_writer(paths)({"design": {"proposal": valid,
                                           "task_brief": valid.get("task_brief", "")}})
            fallback = Path(paths.staging)
            if not _check(fallback, (state.get("design") or {}).get("plan") or {}):
                print("[validator] falling back to the last agent that passed.")
                return _accept(state, merge_section(
                    design, proposal=valid, task_brief=valid.get("task_brief", ""),
                    rationale=valid.get("rationale", "")), fallback, rejected=problems_text)

        return {
            "design": merge_section(design, problems=problems_text),
            "human": merge_section(
                state.get("human"),
                purpose="design_failed",
                question=(
                    f"I could not produce a working agent after {max_attempts} attempts.\n"
                    f"/retry to try again, /discuss to talk it through, "
                    f"/use to run the built-in default agent, or /exit."
                ),
                context=problems_text,
                return_to="human",
            ),
        }

    def _accept(state, design: dict, staging: Path, *, rejected: str = ""):
        # Success. The rename is what makes this durable -- see writer.py.
        target = Path(paths.agent_dir)
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
        print(f"\n[validator] the design is valid. Agent written to {target}")

        # STOP HERE. This used to set outcome="ready", which ended phase 1
        # and started executing immediately -- so the first time you saw
        # the graph was while it was already running. A designed agent is
        # exactly the artifact most worth looking at before it touches
        # anything, and the folder is on disk now, so this is the moment
        # when editing it is both possible and free.
        summary, warnings = _describe(target)
        issues = list(design.get("plan_issues") or [])
        if issues:
            # Not blocking: the designer cannot fix the plan, and the person
            # may accept it as it is. But this is the moment to see it.
            warnings += ("\n\nPLAN ISSUES (the work nodes follow the plan "
                         "exactly; change the plan to change them):\n"
                         + "\n".join(f"  - {i}" for i in issues))
        head = "The agent is designed and written to disk."
        if rejected:
            head = (f"The designer's changes were still rejected after "
                    f"{max_attempts} attempts, so this is the last agent "
                    f"that passed -- the one built from your plan. What "
                    f"was wrong with the changes is at the end.")
            warnings += "\n\nREJECTED CHANGES:\n" + rejected
        return {
            "design": merge_section(design, written_to=str(target), problems=""),
            "human": merge_section(
                state.get("human"),
                purpose="design_review",
                question=(
                    f"{head} Look it over "
                    "before it runs.\n\n/approve to execute it, /retry to "
                    "design again, /revise <what> to change the plan, /discuss "
                    "to talk it through, or /exit.\n\n"
                    "You can also edit the graph first -- in the web UI, or "
                    "by hand in the folder. /approve re-reads it from disk."
                ),
                context=summary + warnings,
                return_to="human",
            ),
        }

    return validator


def _every_step(plan: dict):
    """Top-level steps and their substeps, flat. Ids stay as written, so a
    substep is found by the same `steps` lookup a step is."""
    for step in (plan or {}).get("steps", []):
        yield step
        yield from step.get("substeps", []) or []


def _gate_problems(folder, plan: dict) -> str:
    """Every human-gated plan step must actually reach a human.

    `gate: true` means a person said "stop and ask me before going past
    this". A design that owns that step but has no way to a human node does
    not fail -- it runs straight through, and the approval nobody was asked
    for is discovered afterwards, if at all. Which is the worst shape a bug
    can have: the run looks successful.

    The plan is not part of the folder, so validate_folder cannot see it and
    this check has to live here -- it is the only place both documents are in
    hand.

    Reachability rather than "is there a human node anywhere", because a
    graph usually has one for its exit, and passing on that basis would make
    the check decorative.
    """
    # Substeps too. A research plan's top level is the four STAGES, so the
    # thing a person actually approves -- "start the long training run" -- is
    # nearly always a substep. Walking only the top level would let every gate
    # that matters through unchecked, which is this function's own failure
    # mode: the run looks successful.
    gated = [step for step in _every_step(plan) if step.get("gate")]
    if not gated:
        return ""

    kinds = {ref.name: ref.kind for ref in folder.graph.nodes}
    humans = {name for name, kind in kinds.items() if kind == "human"}

    after: dict[str, set[str]] = {name: set() for name in kinds}
    for edge in folder.graph.edges:
        after.setdefault(edge.from_, set()).add(edge.to)
    for branch in folder.graph.branches:
        after.setdefault(branch.from_, set()).update(
            {case.to for case in branch.cases} | {branch.default})
    # A human node's commands are transitions too, so a gate satisfied only
    # further along a command edge is still satisfied.
    for name in humans:
        config = folder.nodes.get(name)
        for command in getattr(config, "commands", None) or []:
            after.setdefault(name, set()).add(command.to)

    def reaches_human(start: str) -> bool:
        seen, queue = {start}, [start]
        while queue:
            for nxt in after.get(queue.pop(), ()):
                if nxt in humans:
                    return True
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return False

    lines = []
    for step in gated:
        step_id = str(step.get("id", "?"))
        title = str(step.get("title", "")).strip()
        # A gate on a STAGE is satisfied by the stage's work reaching a human,
        # so its substeps' owners count. Nodes own substeps -- a stage is four
        # pieces of work and a node is one -- so without this every gate a
        # person ticks on a stage in the plan editor is unsatisfiable, and the
        # design phase rejects every design with a complaint about a step id
        # that was never meant to be owned directly.
        wanted = {step_id} | {str(sub.get("id", ""))
                              for sub in step.get("substeps", []) or []}
        # And the other way: a node that owns a whole step owns its substeps
        # -- which is how designed agents are built now, one node per step
        # working through its substeps a turn at a time.
        wanted |= {str(parent.get("id", "")) for parent in (plan or {}).get("steps", [])
                   if step_id in {str(s.get("id", "")) for s in parent.get("substeps") or []}}
        owners = [name for name, config in folder.nodes.items()
                  if wanted & set(getattr(config, "steps", None) or [])]

        if not owners:
            lines.append(
                f"- Step {step_id} ({title}) needs a person's approval, and "
                f"no node lists it in `steps`. Give it to a node, and put a "
                f"human node after that node.")
        elif not any(reaches_human(name) for name in owners):
            lines.append(
                f"- Step {step_id} ({title}) needs a person's approval, but "
                f"nothing after {', '.join(sorted(owners))} reaches a human "
                f"node. Add one, with an `ask`, offering /approve and /exit.")

    if not lines:
        return ""
    return ("The plan marks steps as needing human approval, and the graph "
            "does not provide it:\n\n" + "\n".join(lines))


def _describe(folder_path: Path) -> tuple[str, str]:
    """A readable summary of the agent, plus any warnings, for the review gate.

    Warnings are shown HERE and nowhere else in the flow. They are not
    blocking by definition, so without a pause they would scroll past while
    the agent was already running -- which is the same as not reporting them.
    """
    from agent.agentfolder.load import load_agent_folder
    from agent.agentfolder.render import mermaid
    from agent.agentfolder.schema import AgentNodeConfig
    from agent.agentfolder.validate import validate_folder

    try:
        folder = load_agent_folder(folder_path)
    except Exception as exc:  # noqa: BLE001 -- the summary is a nicety
        return f"(could not summarise: {exc})", ""

    from agent.agentfolder.render import find_tokens

    # Who reads whom, from the prompts themselves: what each node is actually
    # given, and which checker looks at each node's work.
    reads: dict[str, set[str]] = {}
    for name, config in folder.nodes.items():
        if isinstance(config, AgentNodeConfig):
            reads[name] = {t.split(".")[1] for t in find_tokens(config.prompts.first)
                           if t.startswith("out.")} - {name}
    checked_by = {n: sorted(c for c, r in reads.items()
                            if n in r and folder.nodes[c].access == "read_only"
                            and not folder.nodes[c].steps)
                  for n in reads}

    lines = [f"{folder.graph.name}: {len(folder.graph.nodes)} nodes", "",
             f"  {'node':34} {'steps':7} {'role (model)':13} {'access':9} "
             f"{'reads':5}  checked by"]
    for ref in folder.graph.nodes:
        config = folder.nodes[ref.name]
        if isinstance(config, AgentNodeConfig):
            checker = ", ".join(checked_by.get(ref.name, [])) or (
                "-" if config.access == "read_only" else "NOBODY")
            lines.append(f"  {ref.name:34} {','.join(config.steps) or '-':7} "
                         f"{config.backend:13} {config.access:9} "
                         f"{len(reads.get(ref.name, ())):5}  {checker}")
        else:
            lines.append(f"  {ref.name:34} human   commands "
                         + ", ".join(f"/{c.name}" for c in config.commands))

    lines += ["", mermaid(folder)]

    warnings = [p for p in validate_folder(folder) if p.warning]
    if not warnings:
        return "\n".join(lines), ""
    return "\n".join(lines), "\n\nWORTH A LOOK:\n" + "\n".join(
        f"  [{p.code}] {p.where}: {p.message}" for p in warnings
    )


def _check(staging: Path, plan: dict | None = None) -> str:
    """Load and validate from disk. Returns "" when the folder is good."""
    if not (staging / "graph.json").exists():
        return "The designer did not produce a folder."

    try:
        folder = load_agent_folder(staging)
    except AgentFolderError as exc:
        return f"The folder could not be read:\n{exc}"

    # strict=True: a design that lets a node grade its own work is rejected
    # here and sent back to the designer, rather than reaching the human. The
    # run-time gate stays lenient so agents designed before that check existed
    # keep working -- see _verification_problems in agentfolder/validate.py.
    problems = [p for p in validate_folder(folder, strict=True) if not p.warning]
    if problems:
        return format_problems(problems)

    # The plan is not part of the folder, so validate_folder cannot see it and
    # this call has to be here. It is the only place both documents are in
    # hand -- and it was missing: _gate_problems shipped with nothing calling
    # it, which its own test did not catch because the test called it
    # directly.
    gates = _gate_problems(folder, plan or {})
    if gates:
        return gates

    # Final smoke test: it must actually compile. Backends are not built yet,
    # so this catches structural problems only -- which is exactly what is
    # left after validate_folder has run.
    try:
        from unittest.mock import MagicMock

        from agent.agentfolder.commands import build_registry
        from agent.work.compile import backends_needed, compile_agent

        compile_agent(
            folder,
            backends={role: MagicMock() for role in backends_needed(folder)},
            checkpointer=None,
            registry=build_registry(folder),
        )
    except Exception as exc:  # noqa: BLE001
        return f"The folder is well-formed but would not compile: {exc}"

    return ""
