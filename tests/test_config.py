"""
WHAT:  The bootstrap configuration: config.toml's defaults, a run's
       config.json, the command line's --role / --skills / --skills-dir, and
       what reaches a node.

Run with:   python -m pytest tests/test_config.py -q
"""

from __future__ import annotations

import json

import pytest

from research_agent import cli
from research_agent.bootstrap import config
from tests.scripted import script  # noqa: F401 -- fixture
from tests.test_graph import say


def test_the_defaults_come_from_config_toml_and_the_field():
    cfg = config.load()
    assert cfg.field == "nlp"
    assert cfg.roles["researcher"] == config.RoleConfig("antigravity", "gemini-3.8-flash-high")
    assert (cfg.roles["discussor"].model, cfg.roles["orchestrator"].model) == (
        "gpt-6.1-sol", "gpt-6.1-sol")
    assert (cfg.roles["designer"].model, cfg.roles["designer"].effort) == ("gpt-6.1-terra", "high")
    assert (cfg.roles["planner"].model, cfg.roles["planner"].effort) == ("gpt-6.1-terra", "high")
    assert cfg.reader == config.RoleConfig("codex", "gpt-6.1-terra", "low")
    # Assigned skills or none: no role discovers skills by default.
    assert cfg.skills["discussor"] is None
    assert cfg.skills["orchestrator"] == ["research-orchestration"]
    assert cfg.skills["researcher"] == ["research"]
    assert cfg.skills["designer"] is None
    assert cfg.skills_dir == config.REPOSITORY / "skills"
    assert set(cfg.roles) == set(config.ROLES) and set(cfg.skills) == set(config.NODES)
    assert config.check(cfg) == [], "every default skill exists"


def test_a_run_s_file_then_the_command_line_win(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "roles": {"designer": {"provider": "claude_code", "model": "sonnet"}},
        "skills": {"designer": ["peft"], "discussor": "none"}}))
    cfg = config.load(tmp_path, roles=["orchestrator=codex:gpt-5.6-luna:low"],
                      skills=["designer=experiment-design,statistical-power",
                              "researcher=auto"])
    assert cfg.roles["designer"] == config.RoleConfig("claude_code", "sonnet", "high")
    assert cfg.roles["orchestrator"] == config.RoleConfig("codex", "gpt-5.6-luna", "low")
    assert cfg.skills["designer"] == ["experiment-design", "statistical-power"]
    assert cfg.skills["discussor"] is None
    assert cfg.skills["researcher"] == "auto"
    every = config.load(tmp_path, roles=["all=claude_code:opus"])
    assert {str(c) for c in every.roles.values()} == {"claude_code:opus"}


def test_an_older_run_s_planner_jobs_are_left_out_and_its_planner_kept(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "roles": {"planner": {"provider": "claude_code", "effort": "high"}},
        "skills": {"planner_plan": ["experiment-design"], "planner_hypothesize": "none"}}))
    cfg = config.load(tmp_path)
    assert cfg.roles["planner"].provider == "claude_code", "the planner is a role again"
    assert "planner_plan" not in cfg.skills and "planner_hypothesize" not in cfg.skills


def test_what_a_run_saves_is_what_it_reads_back(tmp_path):
    cfg = config.load(None, skills=["designer=none", "researcher=research"])
    config.save(tmp_path, cfg)
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["skills"]["designer"] == "none" and saved["skills"]["researcher"] == ["research"]
    assert saved["roles"]["researcher"] == {"provider": "antigravity", "options": {},
                                            "model": "gemini-3.8-flash-high", "effort": None,
                                            "backup": []}
    assert config.load(tmp_path) == cfg
    # A role given without a model stays without one when read back.
    cfg = config.load(None, roles=["discussor=claude_code"])
    config.save(tmp_path, cfg)
    assert config.load(tmp_path).roles["discussor"].model is None


def test_mistakes_are_named(tmp_path):
    for kwargs, message in (
            ({"skills": ["chef=auto"]}, "No node 'chef'"),
            ({"skills": ["designer=nope"]}, "designer: no skill called 'nope'"),
            ({"skills": ["designer"]}, "--skills takes node="),
            ({"roles": ["chef=codex"]}, "No role 'chef'"),
            ({"skills_dir": tmp_path / "missing"}, "no skills folder at")):
        with pytest.raises(ValueError, match=message):
            config.load(None, **kwargs)
    # No skills chosen: no folder needed.
    none = [f"{n}=none" for n in config.NODES]
    assert config.load(None, skills=none, skills_dir=tmp_path / "missing").skills_dir


def test_the_command_line_choice_reaches_the_node(tmp_path, script, monkeypatch, capsys):
    script.add("DiscussorOutput", say("Hi."), say("Hi again."))
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError))
    cli.main(["bootstrap", "idea", "--run", str(tmp_path / "a"), "--role", "all=fake"])
    cli.main(["bootstrap", "idea", "--run", str(tmp_path / "b"), "--role", "all=fake",
              "--skills", "discussor=statistical-power"])
    first, second = script.prompts("DiscussorOutput")
    assert first.startswith("=== THE USER SAYS"), "the default: no skills"
    assert second.startswith("SKILLS FOR THIS TASK") and "statistical-power" in second
    out = capsys.readouterr().out
    assert "Skills (" in out and "discussor=statistical-power" in out
    saved = json.loads((tmp_path / "b" / "config.json").read_text())
    assert saved["skills"]["discussor"] == ["statistical-power"]
