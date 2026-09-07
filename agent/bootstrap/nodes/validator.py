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
            problems_text = _check(staging)

        if not problems_text:
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
            return {
                "design": merge_section(design, written_to=str(target), problems=""),
                "human": merge_section(
                    state.get("human"),
                    purpose="design_review",
                    question=(
                        "The agent is designed and written to disk. Look it over "
                        "before it runs.\n\n/approve to execute it, /retry to "
                        "design again, /discuss to talk it through, or /exit.\n\n"
                        "You can also edit the graph first -- in the web UI, or "
                        "by hand in the folder. /approve re-reads it from disk."
                    ),
                    context=summary + warnings,
                    return_to="human",
                ),
            }

        print(f"\n[validator] the design has problems:\n{problems_text}")

        if attempt < max_attempts:
            print(f"[validator] asking the designer to fix them "
                  f"(attempt {attempt + 1} of {max_attempts})")
            return {"design": merge_section(design, problems=problems_text)}

        print(f"[validator] gave up after {max_attempts} attempts.")
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

    return validator


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
    gated = [step for step in plan.get("steps", []) if step.get("gate")]
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
        owners = [name for name, config in folder.nodes.items()
                  if step_id in (getattr(config, "steps", None) or [])]

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

    lines = [f"{folder.graph.name}: {len(folder.graph.nodes)} nodes", ""]
    for ref in folder.graph.nodes:
        config = folder.nodes[ref.name]
        if isinstance(config, AgentNodeConfig):
            detail = f"{config.backend}/{config.access}"
            if config.steps:
                detail += f" steps {','.join(config.steps)}"
        else:
            detail = "commands " + ", ".join(f"/{c.name}" for c in config.commands)
        lines.append(f"  {ref.name:22} {ref.kind:6} {detail}")

    lines += ["", mermaid(folder)]

    warnings = [p for p in validate_folder(folder) if p.warning]
    if not warnings:
        return "\n".join(lines), ""
    return "\n".join(lines), "\n\nWORTH A LOOK:\n" + "\n".join(
        f"  [{p.code}] {p.where}: {p.message}" for p in warnings
    )


def _check(staging: Path) -> str:
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
