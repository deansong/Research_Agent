"""
WHAT:  The conversation with the discussor, read back from its
       research_backend sessions -- and what a new discussor is given when
       the role moves to another provider.
WHY:   The conversation is the discussor's session. A provider cannot
       continue another provider's thread, so changing the discussor's
       provider (Claude Code -> Codex) starts a new session: the old one is
       kept as discussor-<provider>-NNN, and the new discussor is handed the
       recent conversation and the whole research state.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DISCUSSOR = "discussor"
_USER = re.compile(r"=== THE USER SAYS ===\n(.*?)(?:\n\n=== |\Z)", re.S)
#: Messages, and characters of each, a new discussor is handed.
HANDOVER_MESSAGES = 30
HANDOVER_CHARS = 1500


def sessions(folder: Path) -> list[Path]:
    """The discussor's session files, oldest conversation first."""
    root = Path(folder) / "rb" / "sessions"
    found = [p / "session.json" for p in root.glob(f"{DISCUSSOR}*")
             if (p / "session.json").exists()
             and (p.name == DISCUSSOR or p.name.startswith(f"{DISCUSSOR}-"))]

    def started(path: Path) -> tuple:
        # The current conversation is always the newest, whatever the clock
        # says (two started in the same second).
        try:
            at = json.loads(path.read_text()).get("created_at") or ""
        except ValueError:
            at = ""
        return (path.parent.name == DISCUSSOR, at, path.parent.name)
    return sorted(found, key=started)


def transcript(folder: Path) -> list[dict]:
    """Every message, the user's and the discussor's, across its sessions.
    Retried answers are left out."""
    out = []
    for path in sessions(folder):
        turns = json.loads(path.read_text()).get("turns") or []
        for i, turn in enumerate(turns):
            prompt = turn.get("prompt") or ""
            retried = i + 1 < len(turns) and (turns[i + 1].get("prompt") or "").startswith(
                "Your answer could not be used")
            aside = "=== AN ASIDE" in prompt
            user = _USER.search(prompt)
            if user and not prompt.startswith("Your answer could not be used"):
                said = user.group(1).strip()
                out.append({"role": "user", "text": said, "at": turn.get("at"),
                            "kind": "aside" if aside else "autopilot"
                            if said.startswith("[autopilot]") else "answer"})
            if turn.get("error") or retried:
                continue
            try:
                reply = json.loads(turn.get("reply") or "{}").get("reply", "")
            except ValueError:
                reply = turn.get("reply") or ""
            if reply:
                out.append({"role": "discussor", "text": reply, "at": turn.get("at"),
                            "kind": "aside" if aside else "answer"})
    return out


def handover(folder: Path, budget: int | None = None) -> str:
    """For a discussor taking over the conversation: what was said -- the
    latest messages that fit in `budget` characters."""
    said = transcript(folder)[-HANDOVER_MESSAGES:]
    lines = [f"[{m['role']}{' (aside)' if m['kind'] == 'aside' else ''}] "
             f"{m['text'][:HANDOVER_CHARS]}{'...' if len(m['text']) > HANDOVER_CHARS else ''}"
             for m in said]
    if budget is not None:
        while lines and sum(len(x) + 2 for x in lines) > budget:
            lines.pop(0)
    if not lines:
        return ""
    from research_agent import prompt_files

    return (prompt_files.text("notes/handover", folder, count=len(lines)) + "\n\n"
            + "\n\n".join(lines))
