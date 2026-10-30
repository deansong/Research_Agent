"""
WHAT:  The standing instructions of each bootstrap role, read from
       research_agent/prompts/roles/bootstrap/<role>.md (see prompt_files: a
       run's own prompts/ folder, or $RESEARCH_AGENT_PROMPTS, wins).
WHY:   One place to read what each role is for; editable without code.
CONCEPT: Instructions are sent with every message (Codex hangs past ~6 KB of
       them), so they say only what does not change; everything about this
       study is in the per-call brief (views.py).
"""

from __future__ import annotations

from research_agent import prompt_files

ROLES = ("discussor", "orchestrator", "researcher", "planner", "designer")


def instructions(run_dir=None) -> dict[str, str]:
    """Each role's instructions, as a run (its folder) has them. The
    planner's are for its second job, the steps; its first, the study, has
    its own (`study`)."""
    return {r: prompt_files.instructions(f"roles/bootstrap/{r}", run_dir) for r in ROLES}


def study(run_dir=None) -> str:
    """The planner's instructions for its first job: the study."""
    return prompt_files.instructions("roles/bootstrap/study", run_dir)


#: The defaults, as the files have them now.
INSTRUCTIONS = instructions()
DISCUSSOR, ORCHESTRATOR, RESEARCHER, PLANNER, DESIGNER = (INSTRUCTIONS[r] for r in ROLES)
STUDY = study()
