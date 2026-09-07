"""
WHAT:  Reading and writing the plan and the agent folder over HTTP.
WHY:   Editing is where a UI can silently destroy something. Every check here
       corresponds to a specific way that happens.
CONCEPT: Round-trip, then break it on purpose.

Run with:   python -m pytest webui/tests/test_editing.py -q
"""

from __future__ import annotations

import copy
import json
import os
import pathlib
import sys
import tempfile
import time

import pytest

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from webui import server  # noqa: E402

TIMEOUT = 20.0


def _client(tmp: pathlib.Path) -> TestClient:
    server._RUNNERS.clear()

    class Args:
        backend = "fake"
        model = None
        backend_role = None
        config = None
        session = None

    return TestClient(server.create_app(tmp, Args()))


def _wait(client, sid: str) -> dict:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        detail = client.get(f"/api/sessions/{sid}").json()
        if detail["waiting"]:
            return detail
        if not detail["busy"]:
            raise AssertionError(f"finished early: {detail['error']}")
        time.sleep(0.05)
    raise AssertionError("no question in time")


def _session_with_an_agent(client, repo: pathlib.Path) -> str:
    """Drive the design phase far enough that an agent exists on disk."""
    sid = client.post("/api/sessions", json={
        "repo": str(repo), "task": "survey the repository", "backend": "fake",
    }).json()["id"]
    _wait(client, sid)
    client.post(f"/api/sessions/{sid}/answer", json={"text": "/plan"})
    _wait(client, sid)
    client.post(f"/api/sessions/{sid}/answer", json={"text": "/approve"})
    # A second /approve: the plan, then the designed agent. The design review
    # gate is deliberate -- see test_bootstrap.
    _wait(client, sid)
    client.post(f"/api/sessions/{sid}/answer", json={"text": "/approve"})
    _wait(client, sid)
    client.post(f"/api/sessions/{sid}/answer", json={"text": "/exit"})
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if not client.get(f"/api/sessions/{sid}").json()["busy"]:
            break
        time.sleep(0.05)
    return sid


# ---------------------------------------------------------------------------
# which repository a session is about
# ---------------------------------------------------------------------------


def test_a_relative_repo_is_taken_from_the_launched_repo_not_the_cwd():
    """`python main.py web ../projectA` states which project you mean.

    The bug this pins down: the browser's Repository field defaulted to ".",
    the server resolved that against its own working directory, and a session
    quietly put its `.agent/` beside a different project than the one named on
    the command line. Only visible if you happened to start the server from
    somewhere other than the repo.
    """
    import os

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        project = root / "projectA"
        elsewhere = root / "elsewhere"
        project.mkdir()
        elsewhere.mkdir()

        client = _client(project)          # server started FOR projectA
        previous = os.getcwd()
        os.chdir(elsewhere)                # ...but running from elsewhere
        try:
            detail = client.post("/api/sessions", json={
                "repo": ".", "task": "with the default dot", "backend": "fake",
            }).json()
        finally:
            os.chdir(previous)

        assert detail["repo"] == str(project), detail["repo"]
        assert detail["folder"].startswith(str(project)), detail["folder"]
        assert "elsewhere" not in detail["folder"], detail["folder"]
        print("PASS  a relative repo resolves against the launched repo, not the cwd")


def test_an_absolute_repo_is_honoured_as_typed():
    """One server, several projects -- so an absolute path must win."""
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        launched, other = root / "launched", root / "other"
        launched.mkdir()
        other.mkdir()

        client = _client(launched)
        detail = client.post("/api/sessions", json={
            "repo": str(other), "task": "a different project", "backend": "fake",
        }).json()

        assert detail["repo"] == str(other), detail["repo"]
        assert (other / ".agent" / "sessions").is_dir(), "the session went elsewhere"
        print("PASS  an absolute repo path points the session at that project")


def test_a_missing_repo_says_which_path_and_what_exists_nearby():
    """"Not a directory" is true and useless.

    The real confusion this fixes: the browser showed "bad_repo: Not a
    directory" and dropped `where`, which held the path the server actually
    resolved -- so the one fact that identifies the mistake was the one fact
    not on screen. And for a path that does not exist, the sibling names are
    almost always the answer, because the mistake is nearly always a typo.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "launched").mkdir()
        (root / "emotion_analysis").mkdir()
        (root / "sunday_agent").mkdir()
        client = _client(root / "launched")

        response = client.post("/api/sessions", json={
            "repo": str(root / "my_actual_project"), "task": "x", "backend": "fake",
        })
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "bad_repo"
        # The path it tried, so you can see the typo.
        assert detail["where"] == str(root / "my_actual_project"), detail
        # And what is really there.
        assert "emotion_analysis" in detail["message"], detail["message"]
        assert "sunday_agent" in detail["message"], detail["message"]
        print("PASS  a missing repo names the path tried and its real siblings")


def test_a_file_given_as_a_repo_says_so():
    """The other way to reach bad_repo, with a different fix."""
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "launched").mkdir()
        (root / "notes.txt").write_text("hello")
        client = _client(root / "launched")

        response = client.post("/api/sessions", json={
            "repo": str(root / "notes.txt"), "task": "x", "backend": "fake",
        })
        assert response.status_code == 400, response.text
        message = response.json()["detail"]["message"]
        assert "is a file" in message, message
        print("PASS  a file given as a repo is distinguished from a missing one")


def test_pointing_the_repo_at_dot_agent_is_refused():
    """An easy mistake with a silent, bad outcome.

    The name suggests session files go in `.agent/`, but it is created INSIDE
    the repo -- so this would succeed and give you `.agent/.agent/sessions/`,
    with the agent's read and write access aimed at its own checkpoint and
    definition. Refused rather than warned for that reason.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "launched").mkdir()
        dot_agent = root / "myproject" / ".agent"
        dot_agent.mkdir(parents=True)
        client = _client(root / "launched")

        response = client.post("/api/sessions", json={
            "repo": str(dot_agent), "task": "do a thing", "backend": "fake",
        })
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "repo_is_infrastructure", detail
        # It should name the directory you almost certainly meant.
        assert str(root / "myproject") in detail["message"], detail["message"]

        # Nothing was created -- in particular, no nested .agent/.agent.
        assert not (dot_agent / ".agent").exists()
        print("PASS  a repo pointing at .agent/ is refused, and names what you meant")


def test_an_empty_task_says_what_to_do_about_it():
    """The message a first-time user actually hits."""
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        client = _client(root)
        response = client.post("/api/sessions", json={"repo": str(root), "task": ""})
        assert response.status_code == 400, response.text
        message = response.json()["detail"]["message"]
        assert "want built or changed" in message, message
        print("PASS  an empty task says what to type, not just what is missing")


def test_defaults_endpoint_reports_the_launched_repo():
    """The browser prefills its field from this, so a "." never reaches the
    server from the UI in the first place."""
    with tempfile.TemporaryDirectory() as tmp:
        project = pathlib.Path(tmp) / "projectA"
        project.mkdir()
        client = _client(project)

        got = client.get("/api/defaults").json()
        assert got["repo"] == str(project), got
        assert got["sessions_dir"].startswith(str(project)), got
        assert got["backend"] == "fake", got
        print("PASS  /api/defaults names the repo the server was started for")


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


def test_plan_round_trips_and_validates():
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = client.post("/api/sessions", json={
            "repo": str(repo), "task": "survey the repository", "backend": "fake",
        }).json()["id"]
        _wait(client, sid)
        client.post(f"/api/sessions/{sid}/answer", json={"text": "/plan"})
        _wait(client, sid)

        got = client.get(f"/api/sessions/{sid}/plan").json()
        assert got["exists"] and got["plan"]["steps"], got
        assert got["step_ids"], "the plan should expose its step ids"

        # An edit -- retitling a step -- survives the round trip byte for byte.
        edited = copy.deepcopy(got["plan"])
        edited["steps"][0]["title"] = "Look at absolutely everything"
        saved = client.put(f"/api/sessions/{sid}/plan", json={"plan": edited})
        assert saved.status_code == 200, saved.text

        back = client.get(f"/api/sessions/{sid}/plan").json()["plan"]
        assert back == edited, back

        # And it really is on disk, which is what /approve re-reads.
        folder = pathlib.Path(client.get(f"/api/sessions/{sid}").json()["folder"])
        on_disk = json.loads((folder / "plan.json").read_text())
        assert on_disk["steps"][0]["title"] == "Look at absolutely everything"
        print("PASS  a plan edit round-trips through HTTP and lands on disk")


def test_a_check_and_a_gate_survive_the_round_trip():
    """The two fields the browser is the best place to fix.

    The planner writes a check from what it can infer; the person knows the
    command that actually decides, and knows which steps they want to be
    asked about. So both have to be editable here -- and they have to reach
    disk, because /approve re-reads plan.json and the designer reads it from
    there.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = client.post("/api/sessions", json={
            "repo": str(repo), "task": "run an experiment", "backend": "fake",
        }).json()["id"]
        _wait(client, sid)
        client.post(f"/api/sessions/{sid}/answer", json={"text": "/plan"})
        _wait(client, sid)

        plan = copy.deepcopy(client.get(f"/api/sessions/{sid}/plan").json()["plan"])
        plan["steps"][0]["check"] = "pytest tests/test_sweep.py passes"
        plan["steps"][0]["gate"] = True

        saved = client.put(f"/api/sessions/{sid}/plan", json={"plan": plan})
        assert saved.status_code == 200, saved.text

        folder = pathlib.Path(client.get(f"/api/sessions/{sid}").json()["folder"])
        on_disk = json.loads((folder / "plan.json").read_text())["steps"][0]
        assert on_disk["check"] == "pytest tests/test_sweep.py passes", on_disk
        assert on_disk["gate"] is True, on_disk

        # And the node that owns the step is told both, which is the whole
        # point of putting them in the plan rather than in a comment.
        from agent.bootstrap.nodes.planner import steps_for
        rendered = steps_for(json.loads((folder / "plan.json").read_text()),
                             [on_disk["id"]])
        assert "check: pytest tests/test_sweep.py passes" in rendered, rendered
        assert "human gate" in rendered, rendered
        print("PASS  a check and a gate reach disk and reach the node's prompt")


def test_a_malformed_plan_is_refused_rather_than_written():
    """Refusing here is safe -- a plan has no half-finished state to pass
    through -- and the alternative is a session whose plan.json cannot be read
    back by the graph that needs it."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = client.post("/api/sessions", json={
            "repo": str(repo), "task": "survey the repository", "backend": "fake",
        }).json()["id"]
        _wait(client, sid)
        client.post(f"/api/sessions/{sid}/answer", json={"text": "/plan"})
        _wait(client, sid)

        before = client.get(f"/api/sessions/{sid}/plan").json()["plan"]

        # A step id must be digits; "two" is not.
        broken = copy.deepcopy(before)
        broken["steps"][0]["id"] = "two"
        response = client.put(f"/api/sessions/{sid}/plan", json={"plan": broken})
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "bad_plan"

        assert client.get(f"/api/sessions/{sid}/plan").json()["plan"] == before
        print("PASS  an invalid plan is refused and the old one is untouched")


def test_duplicate_step_ids_are_a_warning_not_a_refusal():
    """Nothing in the agent checks this, and steps_for() would silently hand a
    node both steps -- so the editor is the only place it can be surfaced."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = client.post("/api/sessions", json={
            "repo": str(repo), "task": "survey the repository", "backend": "fake",
        }).json()["id"]
        _wait(client, sid)
        client.post(f"/api/sessions/{sid}/answer", json={"text": "/plan"})
        _wait(client, sid)

        plan = client.get(f"/api/sessions/{sid}/plan").json()["plan"]
        plan["steps"].append({"id": plan["steps"][0]["id"], "title": "a twin",
                              "detail": "", "substeps": []})

        response = client.put(f"/api/sessions/{sid}/plan", json={"plan": plan})
        assert response.status_code == 200, response.text
        codes = [p["code"] for p in response.json()["problems"]]
        assert "duplicate_step_id" in codes, response.json()
        assert all(p["warning"] for p in response.json()["problems"])
        print("PASS  duplicate step ids warn, and the plan still saves")


# ---------------------------------------------------------------------------
# the agent folder
# ---------------------------------------------------------------------------


def test_agent_round_trips_byte_for_byte():
    """GET then PUT unchanged must leave both files identical.

    This is the check that catches the `from` / `from_` alias trap. EdgeSpec
    names the field `from_` because `from` is a Python keyword, with
    alias="from"; a dump without by_alias=True writes "from_" into graph.json,
    which the loader then rejects -- so the agent you just saved will not load.
    Nothing else in the test suite would notice.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = _session_with_an_agent(client, repo)

        detail = client.get(f"/api/sessions/{sid}").json()
        agent_dir = pathlib.Path(detail["folder"]) / "agent"
        before = {p.name: p.read_text() for p in sorted(agent_dir.glob("*.json"))}
        assert set(before) == {"graph.json", "nodes.json"}, before.keys()

        document = client.get(f"/api/sessions/{sid}/agent").json()
        assert '"from"' in json.dumps(document["graph"]), "edges must serialise as 'from'"
        assert "from_" not in json.dumps(document["graph"]), "the alias leaked"

        saved = client.put(f"/api/sessions/{sid}/agent",
                           json={"graph": document["graph"], "nodes": document["nodes"]})
        assert saved.status_code == 200, saved.text

        after = {p.name: p.read_text() for p in sorted(agent_dir.glob("*.json"))}
        assert after == before, "a no-op save changed the files"
        print("PASS  GET then PUT unchanged leaves graph.json and nodes.json identical")


def test_the_shipped_agent_round_trips_unchanged():
    """The strongest version of the round-trip check.

    builtin_agents/default/ is HAND-WRITTEN -- nobody's serialiser produced it.
    If the editor can read and rewrite it byte for byte, then the writing
    convention in agentfolder.schema.node_entry really is the one the format
    uses, rather than one that merely agrees with itself.

    Done directly against editing.save_folder rather than over HTTP, because
    the shipped agent deliberately is not editable through the API -- it is not
    this session's own folder.
    """
    from agent import storage
    from webui import editing

    source = storage.builtin_agents_dir() / "default"
    before = {p.name: p.read_text() for p in sorted(source.glob("*.json"))}

    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "agent"
        target.mkdir()
        for name, text in before.items():
            (target / name).write_text(text)

        document = editing.read_folder(target)
        result = editing.save_folder(
            target, {"graph": document["graph"], "nodes": document["nodes"]},
            staging=pathlib.Path(tmp) / "staging",
        )
        assert result.saved, result.error
        assert not [p for p in result.problems if not p.warning], result.problems

        after = {p.name: p.read_text() for p in sorted(target.glob("*.json"))}

    assert after == before, "rewriting the shipped agent changed it"
    print("PASS  the hand-written default agent survives a read/write round trip")


def test_editing_a_node_parameter_takes_effect():
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = _session_with_an_agent(client, repo)

        document = client.get(f"/api/sessions/{sid}/agent").json()
        worker = next(name for name, cfg in document["nodes"].items() if "backend" in cfg)
        document["nodes"][worker]["backend"] = "a_different_role"
        document["nodes"][worker]["announce"] = "thinking hard..."

        saved = client.put(f"/api/sessions/{sid}/agent",
                           json={"graph": document["graph"], "nodes": document["nodes"]})
        assert saved.status_code == 200, saved.text

        back = client.get(f"/api/sessions/{sid}/agent").json()
        assert back["nodes"][worker]["backend"] == "a_different_role"
        assert back["nodes"][worker]["announce"] == "thinking hard..."
        print("PASS  a node's backend and announce survive an edit")


def test_deleting_a_node_saves_but_reports_the_damage():
    """The save-vs-run asymmetry, which is the whole editing design.

    You cannot rewire a graph without passing through states where something is
    unreachable, so refusing to SAVE those makes the editor unusable. Running is
    what is gated -- cli.py already refuses a folder with blocking problems.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = _session_with_an_agent(client, repo)

        document = client.get(f"/api/sessions/{sid}/agent").json()
        entry = document["graph"]["entry"]
        victim = next(ref["name"] for ref in document["graph"]["nodes"]
                      if ref["name"] != entry)

        document["graph"]["nodes"] = [ref for ref in document["graph"]["nodes"]
                                      if ref["name"] != victim]
        document["nodes"].pop(victim, None)

        response = client.put(f"/api/sessions/{sid}/agent",
                              json={"graph": document["graph"], "nodes": document["nodes"]})
        assert response.status_code == 200, response.text

        codes = {p["code"] for p in response.json()["problems"]}
        # Removing a node leaves the edges that pointed at it dangling.
        assert codes & {"unknown_target", "dead_end", "end_unreachable"}, codes
        assert any(not p["warning"] for p in response.json()["problems"])

        # It really was written -- an editor you cannot leave mid-edit is no use.
        after = client.get(f"/api/sessions/{sid}/agent").json()
        assert victim not in after["nodes"], "the delete should have been saved"
        print("PASS  a destructive edit saves, and reports exactly what it broke")


def test_a_structurally_unreadable_agent_is_refused():
    """Problems are saveable; nonsense is not. A node kind of "wizard" is not a
    state you can be halfway through -- it is a bug in the client."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = _session_with_an_agent(client, repo)

        document = client.get(f"/api/sessions/{sid}/agent").json()
        before = client.get(f"/api/sessions/{sid}/agent").json()
        document["graph"]["nodes"][0]["kind"] = "wizard"

        response = client.put(f"/api/sessions/{sid}/agent",
                              json={"graph": document["graph"], "nodes": document["nodes"]})
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "invalid_agent"

        assert client.get(f"/api/sessions/{sid}/agent").json() == before
        print("PASS  an unparseable agent is refused and the old one survives")


def test_validate_endpoint_does_not_write():
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = _session_with_an_agent(client, repo)

        document = client.get(f"/api/sessions/{sid}/agent").json()
        agent_dir = pathlib.Path(
            client.get(f"/api/sessions/{sid}").json()["folder"]) / "agent"
        before = {p.name: p.read_text() for p in sorted(agent_dir.glob("*.json"))}

        entry = document["graph"]["entry"]
        document["graph"]["nodes"] = [ref for ref in document["graph"]["nodes"]
                                      if ref["name"] == entry]
        document["nodes"] = {entry: document["nodes"][entry]}

        response = client.post(f"/api/sessions/{sid}/agent/validate",
                               json={"graph": document["graph"], "nodes": document["nodes"]})
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is False, response.json()
        assert response.json()["problems"]

        after = {p.name: p.read_text() for p in sorted(agent_dir.glob("*.json"))}
        assert after == before, "validate must not touch the folder"
        print("PASS  /validate reports problems without writing anything")


def test_adding_a_node_and_wiring_it_in_ends_valid():
    """The acceptance test for topology editing.

    Not "does a save succeed" -- saves succeed even when they break things, by
    design -- but "can you get from one working agent to a different working
    agent, through the states in between". Those middle states are invalid, and
    that is the point: an editor that refused them could not do this at all.

    The sequence is the one the Wiring panel performs, in the same order.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = _session_with_an_agent(client, repo)

        agent = client.get(f"/api/sessions/{sid}/agent").json()
        assert not [p for p in agent["problems"] if not p["warning"]], agent["problems"]

        entry = agent["graph"]["entry"]          # "worker"
        human = next(ref["name"] for ref in agent["graph"]["nodes"]
                     if ref["kind"] == "human")  # "review"

        # 1. Add a node. Nothing points at it yet, so this is invalid -- and it
        #    must still save, or you could never build anything.
        graph = copy.deepcopy(agent["graph"])
        nodes = copy.deepcopy(agent["nodes"])
        graph["nodes"].append({"name": "checker", "kind": "agent"})
        nodes["checker"] = {
            "backend": "checker",
            "access": "read_only",
            "output": [{"name": "summary", "type": "string", "required": True}],
            "prompts": {"first": "Check the work.\n\n{task_brief}"},
        }
        response = client.put(f"/api/sessions/{sid}/agent",
                              json={"graph": graph, "nodes": nodes})
        assert response.status_code == 200, response.text
        codes = {p["code"] for p in response.json()["problems"]}
        assert "unreachable" in codes, codes
        assert "dead_end" in codes, codes

        # 2. Wire it between the entry node and the human one.
        graph["edges"] = [e for e in graph["edges"]
                          if not (e["from"] == entry and e["to"] == human)]
        graph["edges"].append({"from": entry, "to": "checker"})
        graph["edges"].append({
            "from": "checker", "to": human,
            "ask": {"purpose": "review", "resume_to": entry,
                    "question": "The checker says: {out.checker.summary}. What next?",
                    "context": ""},
        })
        response = client.put(f"/api/sessions/{sid}/agent",
                              json={"graph": graph, "nodes": nodes})
        assert response.status_code == 200, response.text
        blocking = [p for p in response.json()["problems"] if not p["warning"]]
        assert not blocking, blocking

        # 3. And the result really is a runnable three-node agent.
        after = client.get(f"/api/sessions/{sid}/agent").json()
        assert {n["id"] for n in after["view"]["nodes"]} == {
            entry, "checker", human, "__end__"}
        assert not [p for p in after["problems"] if not p["warning"]]
        print("PASS  add a node, wire it in, and the agent is valid again")


def test_an_edge_into_a_human_node_without_an_ask_is_reported():
    """The failure this catches is silent and confusing: the run stops at a
    human node with no question to show, so the UI just sits there."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = _session_with_an_agent(client, repo)

        agent = client.get(f"/api/sessions/{sid}/agent").json()
        graph = copy.deepcopy(agent["graph"])
        for edge in graph["edges"]:
            edge.pop("ask", None)

        response = client.put(f"/api/sessions/{sid}/agent",
                              json={"graph": graph, "nodes": agent["nodes"]})
        assert response.status_code == 200, response.text
        codes = {p["code"] for p in response.json()["problems"]}
        assert "ask_missing" in codes, codes
        print("PASS  an edge into a human node with no ask is reported")


# ---------------------------------------------------------------------------
# the graph view and node context
# ---------------------------------------------------------------------------


def test_graph_view_includes_human_command_edges():
    """A picture drawn from graph.json alone shows human nodes as dead ends.

    Their exits are slash commands, and those live in nodes.json -- the same
    reason validate.py's reachability walk has to read both files.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = _session_with_an_agent(client, repo)

        view = client.get(f"/api/sessions/{sid}/agent").json()["view"]
        kinds = {edge["kind"] for edge in view["edges"]}
        assert "command" in kinds, kinds
        assert any(n["kind"] == "human" for n in view["nodes"]), view["nodes"]
        assert any(n["kind"] == "end" for n in view["nodes"]), "END must be drawable"
        assert any(n["entry"] for n in view["nodes"]), "the entry node must be marked"
        print("PASS  the graph view carries edges, human commands and __end__")


def test_node_context_renders_placeholders_and_shows_appended_rules():
    """The reason this endpoint exists.

    A prompt in nodes.json is a template; what the model receives is that
    template with placeholders filled in. And a write-access node's real
    instructions are longer than its file, because the compiler appends the
    working rules so a generated agent cannot opt out of them.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = _session_with_an_agent(client, repo)

        document = client.get(f"/api/sessions/{sid}/agent").json()
        worker = next(name for name, cfg in document["nodes"].items() if "backend" in cfg)

        context = client.get(f"/api/sessions/{sid}/nodes/{worker}/context").json()
        assert context["kind"] == "agent"
        template = context["prompts"]["first"]["template"]
        rendered = context["prompts"]["first"]["rendered"]
        assert "{" in template, "the fake agent's prompt should contain placeholders"
        assert "{my_steps}" not in rendered, "placeholders must be resolved"
        assert "{plan_outline}" not in rendered
        assert context["plan_outline"], "the plan outline should be non-empty"

        # Make the node write-access and the working rules must appear.
        document["nodes"][worker]["access"] = "write"
        client.put(f"/api/sessions/{sid}/agent",
                   json={"graph": document["graph"], "nodes": document["nodes"]})
        context = client.get(f"/api/sessions/{sid}/nodes/{worker}/context").json()
        assert "WORKING RULES" in context["appended_instructions"], context
        assert "WORKING RULES" not in context["instructions"], \
            "the appended rules must be shown separately from what is in the file"
        print("PASS  node context resolves placeholders and shows the appended rules")


def test_unknown_step_ids_are_surfaced():
    """steps_for() drops an id it does not recognise, without a word. Nothing in
    the agent checks it, so the editor is the only chance to notice."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = _session_with_an_agent(client, repo)

        document = client.get(f"/api/sessions/{sid}/agent").json()
        worker = next(name for name, cfg in document["nodes"].items() if "backend" in cfg)
        document["nodes"][worker]["steps"] = ["1", "999"]
        client.put(f"/api/sessions/{sid}/agent",
                   json={"graph": document["graph"], "nodes": document["nodes"]})

        view = client.get(f"/api/sessions/{sid}/agent").json()["view"]
        node = next(n for n in view["nodes"] if n["id"] == worker)
        assert node["unknown_steps"] == ["999"], node
        print("PASS  a step id that is not in the plan is reported")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
