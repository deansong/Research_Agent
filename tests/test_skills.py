"""
WHAT:  Shared skills in <repo>/skills/<name>/SKILL.md, and the per-node
       choice -- no skills, auto-discover, or selected skills -- for the
       design agent's nodes and a designed agent's alike, with defaults from
       skills/suggestions.json.
WHY:   A skill nobody receives is a file, and a choice that is saved but not
       sent is worse than none. So each assertion here is about what a node is
       actually SENT.

Run with:   python -m pytest tests/test_skills.py -q
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from agent import activity, skills  # noqa: E402


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """A skills folder of our own: two skills, and suggestions."""
    root = tmp_path / "skills"
    (root / "alpha").mkdir(parents=True)
    (root / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: First skill, for testing.\n---\n\n"
        "ALPHA BODY: run {python} {skill_dir}/tool.py into {artifacts_dir}/out "
        "and keep \\usepackage{acl} as written.\n")
    (root / "beta").mkdir()
    (root / "beta" / "SKILL.md").write_text("---\ndescription: Second.\n---\nBETA BODY\n")
    (root / "suggestions.json").write_text(json.dumps({
        "bootstrap": {"discussor": {"mode": "selected", "skills": ["alpha"]},
                      "planner": {"mode": "auto"}},
        "roles": {"worker": {"mode": "selected", "skills": ["beta"]}}}))
    monkeypatch.setenv("AGENT_SKILLS_DIR", str(root))
    return root


def test_skills_are_discovered_from_their_folders(folder):
    assert skills.names() == ["alpha", "beta"]
    alpha = skills.get("alpha")
    assert alpha.description == "First skill, for testing."
    assert alpha.body.startswith("ALPHA BODY")
    (folder / "gamma").mkdir()
    (folder / "gamma" / "SKILL.md").write_text("---\ndescription: New.\n---\nG\n")
    assert "gamma" in skills.names(), "a new folder shows at once"


def test_each_mode_is_what_the_node_is_sent(folder):
    assert skills.block(None) == "" and skills.block({"mode": "none"}) == ""

    auto = skills.block({"mode": "auto"})
    assert "alpha: First skill, for testing." in auto
    assert str(folder / "beta" / "SKILL.md") in auto
    assert "ALPHA BODY" not in auto, "auto lists, it does not paste"

    chosen = skills.block({"mode": "selected", "skills": ["alpha"]}, artifacts_dir="/runs/a")
    assert "ALPHA BODY" in chosen and "BETA BODY" not in chosen
    assert f"{sys.executable} {folder / 'alpha'}/tool.py" in chosen
    assert "/runs/a/out" in chosen and "\\usepackage{acl}" in chosen


def test_defaults_come_from_suggestions_and_a_broken_file_means_none(folder):
    assert skills.suggested("bootstrap", "discussor") == {"mode": "selected", "skills": ["alpha"]}
    assert skills.suggested("roles", "worker") == {"mode": "selected", "skills": ["beta"]}
    assert skills.suggested("roles", "nobody") == {"mode": "none", "skills": []}
    (folder / "suggestions.json").write_text("{not json")
    assert skills.suggested("bootstrap", "discussor") == {"mode": "none", "skills": []}


def test_the_repository_ships_its_skills_and_defaults(monkeypatch):
    monkeypatch.delenv("AGENT_SKILLS_DIR", raising=False)
    assert {"experiment-design", "research", "acl-paper"} <= set(skills.names())
    assert skills.suggested("bootstrap", "discussor")["skills"] == ["experiment-design"]
    assert skills.suggested("roles", "paper_writer")["skills"] == ["acl-paper"]


# ---- the design agent's nodes ---------------------------------------------------

def test_a_design_agent_nodes_choice_is_saved_as_a_diff_and_checked(folder, tmp_path):
    from agent.bootstrap import settings
    from agent.config import AgentConfig, BackendConfig

    cfg = AgentConfig(default=BackendConfig(provider="fake"), roles={})
    session = tmp_path / "s"
    session.mkdir()
    view = settings.describe(cfg, session)
    assert view["nodes"]["discussor"]["skills"]["mode"] == "selected"
    assert [s["name"] for s in view["available_skills"]] == ["alpha", "beta"]

    # The default again: nothing stored.
    settings.save(session, cfg, {"nodes": {"discussor": {
        "skills": {"mode": "selected", "skills": ["alpha"]}}}})
    assert not settings.path_for(session).exists()

    settings.save(session, cfg, {"nodes": {"planner": {
        "skills": {"mode": "selected", "skills": ["beta"]}}}})
    stored = json.loads(settings.path_for(session).read_text())
    assert stored["nodes"]["planner"] == {"skills": {"mode": "selected", "skills": ["beta"]}}
    assert settings.effective_skills(settings.load(session))["planner"]["skills"] == ["beta"]

    with pytest.raises(settings.SettingsError, match="No skill called 'nope'"):
        settings.save(session, cfg, {"nodes": {"planner": {
            "skills": {"mode": "selected", "skills": ["nope"]}}}})


def test_an_old_checklist_override_does_not_stop_a_session(tmp_path):
    from agent.bootstrap import settings

    (tmp_path / "bootstrap.json").write_text(json.dumps({"nodes": {"discussor": {
        "prompts": {"checklist": "my old checklist"}}}}))
    assert settings.load(tmp_path).nodes["discussor"].prompts == {}


def test_the_discussor_and_planner_are_sent_their_skills(folder):
    from tests.test_supervisor import _answer, _client, _until
    from webui.tests.conftest import temp_repo

    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo, "off")
        sid = client.post("/api/sessions", json={
            "repo": str(repo), "task": "survey it", "backend": "fake",
            "research": False}).json()["id"]
        session = repo / ".agent" / "sessions" / sid
        _until(client, sid, purpose="discussion")
        discussor = activity.latest(session, "discussor").sent["prompt"]
        assert "ALPHA BODY" in discussor, "selected: in full"
        _answer(client, sid, "/plan")
        _until(client, sid, purpose="plan_review")
        planner = activity.latest(session, "planner").sent["prompt"]
        assert "SKILLS YOU CAN USE" in planner and "beta: Second." in planner, "auto: listed"
        designer_prompt = None
        _answer(client, sid, "/approve")
        _until(client, sid, purpose="design_review")
        designer_prompt = activity.latest(session, "designer").sent["prompt"]
        assert "SKILLS THE NODES CAN BE GIVEN" in designer_prompt
        assert "alpha: First skill" in designer_prompt

        # The designed nodes got the role defaults: worker -> selected [beta].
        nodes = json.loads((session / "agent" / "nodes.json").read_text())
        work = [n for n, e in nodes.items() if n.startswith("s1_")][0]
        assert nodes[work]["skills"] == {"mode": "selected", "skills": ["beta"]}


# ---- a designed agent's nodes -------------------------------------------------

def test_a_designed_node_gets_its_skills_in_its_first_prompt_only(folder, tmp_path):
    from tests.test_orchestrator import _folder, _run

    def give(graph, nodes):
        nodes["s1_data"]["skills"] = {"mode": "selected", "skills": ["alpha"]}
        nodes["validator"]["skills"] = {"mode": "auto"}

    _run(tmp_path, _folder(tmp_path, give))
    s1 = activity.turns(str(tmp_path / "session"), "s1_data", limit=10)
    first = [t for t in s1 if t.sent["template"] == "first"]
    assert first and all("ALPHA BODY" in t.sent["prompt"] for t in first)
    assert f"{tmp_path / 'artifacts'}/out" in first[0].sent["prompt"], "tokens filled at run time"
    validator = activity.turns(str(tmp_path / "session"), "validator", limit=10)
    assert "SKILLS YOU CAN USE" in validator[0].sent["prompt"]
    s2 = activity.turns(str(tmp_path / "session"), "s2_eval", limit=10)
    assert "SKILLS" not in s2[0].sent["prompt"], "no skills chosen: none sent"


def test_a_designed_node_naming_a_missing_skill_is_rejected_or_warned(folder, tmp_path):
    from agent.agentfolder.validate import validate_folder
    from agent.bootstrap import blueprint
    from agent.bootstrap.spec import SpecError, compile_design
    from tests.test_orchestrator import PLAN, _folder

    design = blueprint.starting_design(PLAN, request="x")
    design["nodes"][1].update(skill_mode="selected", skills=["nope"])
    with pytest.raises(SpecError, match="names the skill 'nope'"):
        compile_design(design, PLAN)

    def give(graph, nodes):
        nodes["s1_data"]["skills"] = {"mode": "selected", "skills": ["gone"]}

    problems = validate_folder(_folder(tmp_path, give))
    assert [p.code for p in problems] == ["skill_missing"] and problems[0].warning


def test_every_provider_may_read_the_skills_folder(folder):
    from agent.backends.base import Access
    from agent.backends.claude_code import ClaudeCodeBackend

    backend = ClaudeCodeBackend.__new__(ClaudeCodeBackend)
    backend.writable_roots = ["/s/artifacts"]
    backend.readable_roots = [str(folder)]
    flags = backend.access_flags(Access.READ_ONLY)
    assert flags[-4:] == ["--add-dir", "/s/artifacts", "--add-dir", str(folder)]

    from agent import storage

    paths = storage.session_paths(folder.parent, "s1")
    storage.create_workspace(paths)
    assert f"read the shared skills in {folder}" in storage.isolation_rules(paths)


def test_the_skills_api_lists_them(folder, tmp_path):
    from tests.test_supervisor import _client

    body = _client(tmp_path, "off").get("/api/skills").json()
    assert body["dir"] == str(folder)
    assert [s["name"] for s in body["skills"]] == ["alpha", "beta"]
    assert body["suggestions"]["roles"]["worker"]["skills"] == ["beta"]


def test_a_long_description_in_yaml_block_form_is_read(folder):
    (folder / "gamma").mkdir()
    (folder / "gamma" / "SKILL.md").write_text(
        "---\nname: gamma\ndescription: >\n  Spans two\n  lines.\nlicense: MIT\n---\nG\n")
    gamma = skills.get("gamma")
    assert gamma.description == "Spans two lines." and gamma.body == "G"
