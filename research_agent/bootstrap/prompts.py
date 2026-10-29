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

ROLES = ("discussor", "orchestrator", "researcher", "designer")


def instructions(run_dir=None) -> dict[str, str]:
    """Each role's instructions, as a run (its folder) has them."""
    return {r: prompt_files.text(f"roles/bootstrap/{r}", run_dir) for r in ROLES}


#: The defaults, as the files have them now.
INSTRUCTIONS = instructions()
DISCUSSOR, ORCHESTRATOR, RESEARCHER, DESIGNER = (INSTRUCTIONS[r] for r in ROLES)
