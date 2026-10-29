"""
WHAT:  The standing instructions of each execution role, read from
       research_agent/prompts/roles/execute/<role>.md (see prompt_files: a
       run's own prompts/ folder, or $RESEARCH_AGENT_PROMPTS, wins).
WHY:   One place to read what each role is for. What the approved design says
       about a node (its own instructions) goes in that node's brief, so the
       standing instructions stay short and the same for every study.
"""

from __future__ import annotations

from research_agent import prompt_files

#: Graph role -> its file; every step owner (worker, writer) shares one.
FILES = {"discussor": "discussor", "orchestrator": "orchestrator", "worker": "owner",
         "writer": "owner", "validator": "validator", "reviewer": "reviewer"}


def instructions(run_dir=None) -> dict[str, str]:
    """Each role's instructions, as a run (its folder) has them."""
    return {role: prompt_files.text(f"roles/execute/{name}", run_dir)
            for role, name in FILES.items()}


#: The defaults, as the files have them now.
INSTRUCTIONS = instructions()
DISCUSSOR, ORCHESTRATOR, OWNER, VALIDATOR, REVIEWER = (
    INSTRUCTIONS[r] for r in ("discussor", "orchestrator", "worker", "validator", "reviewer"))
