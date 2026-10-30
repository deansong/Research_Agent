"""
WHAT:  The prompts read from Markdown files: every default loads, a run's own
       prompts/ folder and $RESEARCH_AGENT_PROMPTS win over the defaults, and
       placeholders are filled while the file's comment is never sent.

Run with:   python -m pytest tests/test_prompt_files.py -q
"""

from __future__ import annotations

from research_agent import prompt_files
from research_agent.bootstrap import nodes, prompts
from research_agent.bootstrap.config import ROLES, RoleConfig


def test_every_default_prompt_loads_without_its_comment():
    names = prompt_files.names()
    assert "roles/bootstrap/designer" in names and "notes/design_ready" in names
    for name in names:
        text = prompt_files.text(name)
        assert text and "<!--" not in text, name


def test_placeholders_are_filled_and_a_dollar_can_be_written():
    assert prompt_files.text("notes/design_ready", version=3).startswith(
        "The designer delivered design v3;")
    assert "$step" in prompt_files.text("notes/run_not_ask"), "unfilled ones stay visible"


def test_a_run_s_own_prompt_wins_then_the_user_s_folder(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    mine = tmp_path / "mine" / "roles" / "bootstrap"
    mine.mkdir(parents=True)
    (mine / "designer.md").write_text("<!-- mine -->\nYou are MY designer.")
    monkeypatch.setenv(prompt_files.ENV, str(tmp_path / "mine"))
    assert prompts.instructions(run_dir)["designer"].startswith("You are MY designer.\n\n")
    assert prompts.instructions(run_dir)["discussor"] == prompts.DISCUSSOR, "the rest stay"
    own = run_dir / "prompts" / "roles" / "bootstrap"
    own.mkdir(parents=True)
    (own / "designer.md").write_text("You are this run's designer.")
    run = nodes.Run(folder=run_dir, roles={r: RoleConfig("fake") for r in ROLES})
    assert run.instructions["designer"].startswith("You are this run's designer.\n\n")
    assert prompt_files.path("roles/bootstrap/designer", run_dir) == own / "designer.md"
