"""
WHAT:  Research fields: a run's field picks the skills its team may use, the
       built-in roles' default skills, and the voice every role speaks in.

Run with:   python -m pytest tests/test_fields.py -q
"""

from __future__ import annotations

import json

import pytest

from research_agent import fields, prompt_files
from research_agent.bootstrap import config, nodes, prompts
from research_agent.bootstrap.config import RoleConfig
from research_agent.execute import config as exec_config


def test_the_nlp_field_is_the_default_and_lists_its_skills():
    assert fields.names() == ["nlp"] and fields.DEFAULT == "nlp"
    nlp = fields.load("nlp")
    assert "acl-paper" in nlp.skills and "research" in nlp.skills
    assert nlp.roles["discussor"] == [] and nlp.roles["researcher"] == ["research"]
    with pytest.raises(ValueError, match="No field 'biology'"):
        fields.load("biology")


def test_a_run_s_field_sets_its_skills_and_voice(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    assert fields.of_run(run) == "nlp", "a run without one is NLP"
    cfg = config.load(run)
    config.save(run, cfg)
    assert json.loads((run / "config.json").read_text())["field"] == "nlp"
    assert exec_config.load(run).paper_skills == ("acl-paper", "ml-paper-writing")
    voice = prompt_files.text(prompt_files.VOICE, run)
    assert "ACL or EMNLP" in voice and prompts.instructions(run)["designer"].endswith(voice)


def test_the_catalogue_holds_only_the_field_s_skills(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    (run / "config.json").write_text(json.dumps({"field": "nlp"}))
    monkeypatch.setattr(fields, "load", lambda name="nlp": fields.Field(
        name="nlp", title="t", skills=("peft",), roles={}))
    r = nodes.Run(folder=run, roles={"designer": RoleConfig("fake")},
                  skills_dir=config.REPOSITORY / "skills")
    assert [name for name, _ in r.catalogue()] == ["peft"]
    with pytest.raises(ValueError, match="designer: 'vllm' is not in the nlp field's skills"):
        config.load(run, skills=["designer=peft,vllm"])     # a skill, but not the field's
