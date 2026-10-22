"""
WHAT:  The research agent between the discussor and you: asked before the
       human when research is on, never when it is off; a failed round does
       not stop the discussion; and the paper lookups parse what the services
       return (and refuse what they get wrong).
WHY:   Research is a help, not a gate -- every way it can fail must leave the
       discussion going.

Run with:   python -m pytest tests/test_research.py -q
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from agent import activity  # noqa: E402
import importlib.util  # noqa: E402

from agent import skills as skill_lib  # noqa: E402

# The research skill's script lives in its skill folder, not in a package.
_SPEC = importlib.util.spec_from_file_location(
    "research_tools", skill_lib.skills_dir() / "research" / "research_tools.py")
tools = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tools)


def _session(client, repo, **extra):
    sid = client.post("/api/sessions", json={
        "repo": str(repo), "task": "compare LoRA with a probe", "backend": "fake",
        **extra}).json()["id"]
    return sid, repo / ".agent" / "sessions" / sid


def test_with_research_on_the_discussor_asks_the_researcher_before_you(tmp_path):
    from tests.test_supervisor import _client, _until
    from webui.tests.conftest import temp_repo

    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo, "off")
        sid, session = _session(client, repo, research=True)
        asked = _until(client, sid, purpose="discussion")

        roles = [t["role"] for t in client.get(f"/api/sessions/{sid}").json()["transcript"]]
        assert roles[:4] == ["human", "discussor", "researcher", "discussor"], roles
        assert (session / "research" / "round-1.md").exists(), "a report to read"
        second = activity.latest(session, "discussor").sent["prompt"]
        assert "The research agent answered" in second
        assert "research_tools.py" in activity.latest(session, "researcher").sent["prompt"]
        assert asked["pending"]["question"], "and then you are asked"

        # One round per human turn: answering does not loop back forever.
        client.post(f"/api/sessions/{sid}/answer", json={"text": "1: yes"})
        _until(client, sid, purpose="discussion")
        assert json.loads((session / "options.json").read_text())["research"] is True


def test_with_research_off_it_is_never_asked(tmp_path):
    from tests.test_supervisor import _client, _until
    from webui.tests.conftest import temp_repo

    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo, "off")
        sid, session = _session(client, repo, research=False)
        _until(client, sid, purpose="discussion")
        roles = [t["role"] for t in client.get(f"/api/sessions/{sid}").json()["transcript"]]
        assert "researcher" not in roles, roles
        assert not (session / "research").exists()
        assert json.loads((session / "options.json").read_text())["research"] is False
        assert "not available" in activity.latest(session, "discussor").sent["prompt"]


def test_a_failed_research_round_is_reported_and_the_discussion_goes_on(monkeypatch):
    from agent.backends.base import BackendTimeout
    from agent.backends.fake import FakeBackend
    from agent.bootstrap.schemas import ResearchReport
    from tests.test_supervisor import _client, _until
    from webui.tests.conftest import temp_repo

    original = FakeBackend.run_structured

    def failing(self, **kw):
        if kw["output_model"] is ResearchReport:
            raise BackendTimeout("agy went quiet")
        return original(self, **kw)

    monkeypatch.setattr(FakeBackend, "run_structured", failing)
    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo, "off")
        sid, session = _session(client, repo, research=True)
        _until(client, sid, purpose="discussion")
        transcript = client.get(f"/api/sessions/{sid}").json()["transcript"]
        research = [t["text"] for t in transcript if t["role"] == "researcher"]
        assert research and "failed (BackendTimeout)" in research[0]


# ---- the tools, offline -----------------------------------------------------

def test_papers_and_arxiv_parse_what_the_services_return(monkeypatch):
    hf = json.dumps([{"paper": {"id": "2106.09685", "title": "LoRA", "publishedAt":
                                "2021-06-17T00:00:00Z", "upvotes": 9, "summary": "x " * 300}}])
    atom = b"""<?xml version='1.0'?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <id>http://arxiv.org/abs/2106.09685v2</id><title>LoRA</title>
      <published>2021-06-17T17:37:18Z</published><summary>low rank</summary>
      <author><name>Edward Hu</name></author></entry></feed>"""
    monkeypatch.setattr(tools, "_get", lambda url, **kw: atom if "arxiv.org" in url
                        else hf.encode())

    found = tools.papers("lora", 5)[0]
    assert found["arxiv"] == "2106.09685" and found["published"] == "2021-06-17"
    assert len(found["summary"]) <= 320, "summaries are clipped"
    entry = tools.arxiv("lora", 5)[0]
    assert entry["arxiv"] == "2106.09685v2" and entry["authors"] == "Edward Hu"


def test_cites_refuses_a_record_whose_title_does_not_match(monkeypatch):
    """Measured: OpenAlex's record for LoRA carries another paper's title, and
    so another paper's citation count."""
    monkeypatch.setattr(tools, "paper", lambda i, n: [{"title": "LoRA: Low-Rank Adaptation"}])
    monkeypatch.setattr(tools, "_json", lambda url: {"results": [
        {"title": "LoRA Fine-Tuning of a 3B Code LLM", "cited_by_count": 2493, "id": "W1"}]})
    out = tools.cites("2106.09685", 3)
    assert "cited_by" not in out[0] and "unknown" in out[0]["note"]


def test_the_cli_prints_rows_and_json(monkeypatch, capsys):
    monkeypatch.setattr(tools, "_get", lambda url, **kw: json.dumps(
        [{"id": "stanfordnlp/imdb", "downloads": 5, "likes": 1, "tags": ["license:other"]}]
    ).encode())
    assert tools.main(["datasets", "imdb"]) == 0
    assert "id: stanfordnlp/imdb" in capsys.readouterr().out
    tools.main(["datasets", "imdb", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["url"].endswith("datasets/stanfordnlp/imdb")


def test_the_skills_name_the_tools_by_absolute_path():
    """The research agent runs in a copy of the user's project, where this
    package is not importable."""
    from agent import skills

    text = skills.research()
    assert str(tools.__file__) in text and "{python}" not in text and "{skill_dir}" not in text
    assert "papers" in text and "datasets" in text
    with pytest.raises(SystemExit):
        tools.main(["nope", "x"])


def test_the_buttons_under_a_question_survive_a_refresh():
    """The page redraws the question from the session's details on every
    refresh. Those details carried no commands, so the buttons under the
    question vanished a moment after they appeared, and never came back
    after a reload."""
    from tests.test_supervisor import _client, _until
    from webui.tests.conftest import temp_repo

    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo, "off")
        sid, _ = _session(client, repo, research=False)
        detail = _until(client, sid, purpose="discussion")
        assert [c["name"] for c in detail["commands"]] == ["plan", "exit"], detail["commands"]
        client.post(f"/api/sessions/{sid}/answer", json={"text": "/plan"})
        detail = _until(client, sid, purpose="plan_review")
        assert {"approve", "revise", "discuss"} <= {c["name"] for c in detail["commands"]}


def test_a_role_of_the_designed_agent_can_be_moved_to_another_model():
    """There was nowhere to change which model a designed node runs on once
    the session had an agent. Now: PUT the role's provider and model; it is
    saved with the session, shown at once, and refused when the provider
    cannot do the role's job."""
    from tests.test_supervisor import _answer, _client, _until
    from webui.tests.conftest import temp_repo

    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo, "off")
        sid, session = _session(client, repo, research=False)
        _until(client, sid, purpose="discussion")
        _answer(client, sid, "/plan")
        _until(client, sid, purpose="plan_review")
        _answer(client, sid, "/approve")
        _until(client, sid, purpose="design_review")

        agent = client.get(f"/api/sessions/{sid}/agent").json()
        roles = agent["backends"]
        reader = next(r for r, b in roles.items() if "checker" in r or "check" in str(b["nodes"]))
        assert roles[reader]["nodes"], roles

        moved = client.put(f"/api/sessions/{sid}/roles/{reader}",
                           json={"provider": "claude_code", "model": "opus"})
        assert moved.status_code == 200, moved.text
        now = moved.json()["backends"][reader]
        assert (now["provider"], now["model"], now["session_choice"]) == ("claude_code", "opus", True)
        assert f"{reader}=claude_code:opus" in json.loads(
            (session / "options.json").read_text())["backend_role"]
        shown = client.get(f"/api/sessions/{sid}/agent").json()["backends"][reader]
        assert shown["provider"] == "claude_code", "the graph view shows it too"

        # antigravity cannot run a read-only role: refused, with the reason.
        refused = client.put(f"/api/sessions/{sid}/roles/{reader}",
                             json={"provider": "antigravity"})
        assert refused.status_code == 422 and "read_only" in refused.text, refused.text

        # An empty provider takes the session's choice back off.
        back = client.put(f"/api/sessions/{sid}/roles/{reader}", json={"provider": ""})
        assert back.json()["backends"][reader]["session_choice"] is False
        assert client.put(f"/api/sessions/{sid}/roles/nobody",
                          json={"provider": "codex"}).status_code == 404

        # And the run uses it: the node's turns record the model they ran on.
        assert client.put(f"/api/sessions/{sid}/roles/{reader}", json={
            "provider": "fake", "model": "swapped-model"}).status_code == 200
        _answer(client, sid, "/approve")
        _until(client, sid, purpose="findings")
        node = roles[reader]["nodes"][0]
        assert activity.latest(session, node).model == "swapped-model"
