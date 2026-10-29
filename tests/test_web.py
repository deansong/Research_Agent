"""
WHAT:  The web UI's server on scripted answers: starting a run, the
       conversation (answers and asides), approvals, the research state and
       its history, the event stream, files, sessions, and starting the
       execution.

Run with:   python -m pytest tests/test_web.py -q
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from research_agent import state as S
from research_agent.web import server
from tests.scripted import script  # noqa: F401 -- fixture
from tests.test_execute_graph import PAPER, REVIEW, reviewed, approved_state, done_writing, verdict
from tests.test_execute_graph import decide as move, say as tell
from tests.test_graph import FINDINGS, decide, say
from dataclasses import replace


@pytest.fixture
def web(tmp_path):
    app = server.create_app(tmp_path / "runs", roles=["all=fake"], watch=True)
    with TestClient(app) as client:
        client.root = tmp_path / "runs"
        client.app_ = app
        yield client
    for host in app.state.hosts.values():
        if host._worker:
            host._worker.join(10)


def settle(web, run_id, seconds=10):
    """Wait until the run's worker and asides are done."""
    end = time.time() + seconds
    while time.time() < end:
        d = web.get(f"/api/runs/{run_id}").json()
        if not d["busy"] and not d["asides"]:
            return d
        time.sleep(0.05)
    raise AssertionError(f"{run_id} still working: {d}")


def test_a_run_from_the_page(web, script):
    script.add("DiscussorOutput", say("Researching first.", ("research", "survey")),
               say("Found 1 source. What budget?"), say("One GPU, noted."))
    script.add("ResearcherOutput", FINDINGS)
    script.add("OrchestratorOutput", decide("research", questions=["q"]),
               decide("ask_user", "What budget?"))
    r = web.post("/api/runs", json={"idea": "Does LoRA rank matter?", "name": "lora"})
    assert r.status_code == 200 and r.json()["id"] == "lora"
    d = settle(web, "lora")
    assert d["waiting"] and d["stage"] == "bootstrap" and d["started"]
    chat = [(m["role"], m["text"]) for m in d["transcript"]]
    assert chat == [("user", "Does LoRA rank matter?"), ("discussor", "Researching first."),
                    ("discussor", "Found 1 source. What budget?")]

    assert web.post("/api/runs/lora/message", json={"text": "One GPU"}).json()["as"] == "answer"
    d = settle(web, "lora")
    assert d["transcript"][-1]["text"] == "One GPU, noted."

    st = web.get("/api/runs/lora/state").json()
    assert st["state"]["evidence"][0]["id"] == "E1" and "## Evidence" in st["markdown"]
    assert st["approved"] == {s: False for s in ("brief", *S.PARTS)}
    assert st["approvable"] == ["brief", "design"]
    hist = web.get("/api/runs/lora/history").json()
    assert hist and hist[0]["n"] == 1 and "by" in hist[0]
    entry = web.get(f"/api/runs/lora/history/{hist[-1]['n']}").json()
    assert "ops" in entry

    runs = web.get("/api/runs").json()
    assert runs[0]["id"] == "lora" and runs[0]["open"] and runs[0]["stage"] == "bootstrap"
    sessions = {s["id"] for s in web.get("/api/runs/lora/sessions").json()}
    assert {"discussor", "orchestrator-001", "researcher-001"} <= sessions
    turns = web.get("/api/runs/lora/sessions/researcher-001").json()["turns"]
    assert turns[0]["prompt"] and json.loads(turns[0]["reply"])["summary"]
    g = web.get("/api/runs/lora/graph").json()
    assert {n["id"] for n in g["nodes"]} >= {"discussor", "orchestrator", "researcher"}

    # The event stream: replayable from any seq; status, chat, call, step and state events.
    text = web.get("/api/runs/lora/events?follow=false").text
    kinds = set(re.findall(r"^event: (\w+)$", text, re.M))
    assert {"status", "chat", "call", "step", "state"} <= kinds
    seqs = [int(x) for x in re.findall(r"^id: (\d+)$", text, re.M)]
    later = web.get(f"/api/runs/lora/events?follow=false&after_seq={seqs[-2]}").text
    assert re.findall(r"^id: (\d+)$", later, re.M) == [str(seqs[-1])]


import re  # noqa: E402


def test_the_discussor_answers_while_the_team_works(web, script):
    searching, answered = threading.Event(), threading.Event()

    def research(prompt):
        searching.set()
        assert answered.wait(10)
        return FINDINGS

    def side(prompt):
        answered.set()
        return {"reply": "The researcher is surveying; I'll add text-only data.",
                "requests": [{"kind": "research", "detail": "Include text-only datasets"}]}

    script.add("DiscussorOutput", say("Researching.", ("research", "survey")), say("Done."))
    script.add("ResearcherOutput", research)
    script.add("BootstrapAside", side)
    script.add("OrchestratorOutput", decide("research", questions=["q"]),
               decide("ask_user", "ok?"))
    web.post("/api/runs", json={"idea": "Emotion classification", "name": "emo"})
    assert searching.wait(10)
    d = web.get("/api/runs/emo").json()
    assert d["busy"] and "researcher" in d["current"]
    r = web.post("/api/runs/emo/message", json={"text": "How is it going?"})
    assert r.json()["as"] == "aside"
    d = settle(web, "emo")
    texts = [(m["role"], m["kind"], m["text"]) for m in d["transcript"]]
    assert ("user", "aside", "How is it going?") in texts
    assert ("discussor", "aside", "The researcher is surveying; I'll add text-only data.") in texts
    assert "- [research] Include text-only datasets" in script.prompts("OrchestratorOutput")[1]
    events = web.get("/api/runs/emo/events?follow=false").text
    assert '"noted": ["request: Include text-only datasets"]' in events


def test_approve_then_execute_from_the_page(web, script):
    folder = web.root / "ready"
    st = approved_state()
    st["approvals"] = {k: v for k, v in st["approvals"].items() if k != "design"}
    S.Store(folder).write(st)
    (folder / "execute.json").unlink(missing_ok=True)
    script.add("DiscussorOutput", say("The design is ready. Approve?"), say("Approved. Done."))
    script.add("OrchestratorOutput", decide("ask_user", "Approve the design?"))
    web.post("/api/runs/ready/message", json={"text": "hello"})
    d = settle(web, "ready")
    assert d["waiting"] and not d["can_execute"]
    assert web.post("/api/runs/ready/approve", json={"sections": ["nope"]}).status_code == 400
    assert web.post("/api/runs/ready/approve", json={"sections": ["design"]}).status_code == 200
    d = settle(web, "ready")
    assert d["can_execute"] and d["outcome"] == "ready"
    now = web.get("/api/runs/ready/graph").json()
    planned = web.get("/api/runs/ready/graph", params={"view": "execution"}).json()
    assert "researcher" in {n["id"] for n in now["nodes"]} and now["can_preview"] and not now["preview"]
    assert {"builder", "trainer", "validator", "reviewer"} <= {n["id"] for n in planned["nodes"]}
    assert planned["preview"], "the execution graph, before the execution starts"

    (folder / "execute.json").write_text(json.dumps({"execute": {"poll_seconds": 0.05}}))
    (folder / "execute.json").unlink()
    script.add("ExecDecision", move("run", "1"), move("run", "2"), move("run", "2"), move("done"))
    script.add("StepReport", done_writing(_Folder(folder), "1"), done_writing(_Folder(folder), "2"))
    script.add("Verdict", verdict(), verdict())
    script.add("PaperReport", PAPER)
    reviewed(script)
    script.add("ExecDiscussorOutput", tell("Step 2 waits for your go."), tell("All done; 5/10."))
    r = web.post("/api/runs/ready/execute", json={"gpu_hours": 10})
    assert r.status_code == 200 and r.json()["stage"] == "execution"
    d = settle(web, "ready")
    assert d["awaiting"] == "2" and d["waiting"]
    assert web.post("/api/runs/ready/break", json={"step": "9", "when": "after"}).status_code == 400
    assert web.post("/api/runs/ready/go").status_code == 200
    d = settle(web, "ready")
    assert d["outcome"] == "done" and d["transcript"][-1]["text"] == "All done; 5/10."
    files = {f["path"] for f in web.get("/api/runs/ready/files").json()["files"]}
    assert {"artifacts/step_01/out.txt", "reviews/V1.md", "state/research.md"} <= files
    assert not any(p.startswith("rb/") for p in files)
    shown = web.get("/api/runs/ready/file", params={"path": "reviews/V1.md"}).json()
    assert "[must] Run five seeds" in shown["text"]
    assert web.get("/api/runs/ready/file", params={"path": "../../etc/passwd"}).status_code == 404
    exe = json.loads((folder / "execute.json").read_text())
    assert exe["execute"]["gpu_hours"] == 10
    g = web.get("/api/runs/ready/graph").json()
    statuses = {n["id"]: n.get("status") for n in g["nodes"]}
    assert statuses["builder"] == "pass" and "reviewer" in statuses

    reviewed(script, {**REVIEW, "rating": 7})
    script.add("ExecDiscussorOutput", tell("Second review: 7/10."))
    assert web.post("/api/runs/ready/review").status_code == 200
    d = settle(web, "ready")
    assert d["transcript"][-1]["text"] == "Second review: 7/10." and d["waiting"]
    st = web.get("/api/runs/ready/state").json()["state"]
    assert [v["rating"] for v in st["reviews"]] == [REVIEW["rating"], 7]
    assert (folder / "reviews" / "V2.md").exists()

    script.add("PaperReport", {**PAPER, "summary": "Rewritten."})
    reviewed(script, {**REVIEW, "rating": 8})
    script.add("ExecDiscussorOutput", tell("New paper, 8/10."))
    assert web.post("/api/runs/ready/paper").status_code == 200
    d = settle(web, "ready")
    assert d["transcript"][-1]["text"] == "New paper, 8/10." and d["waiting"]
    assert [v["rating"] for v in web.get("/api/runs/ready/state").json()["state"]["reviews"]][-1] == 8

    script.add("DiscussorOutput", say("The review asks for five seeds. Shall we?"))
    r = web.post("/api/runs/ready/redesign")
    assert r.status_code == 200 and r.json()["id"] == "ready-next"
    d = settle(web, "ready-next")
    assert d["stage"] == "bootstrap" and d["waiting"]
    assert "Go through its paper and the review" in d["transcript"][0]["text"]
    assert d["transcript"][-1]["text"] == "The review asks for five seeds. Shall we?"
    new = web.get("/api/runs/ready-next/state").json()["state"]
    assert new["status"]["previous"] == str(folder.resolve()) and len(new["reviews"]) == 3


class _Folder:
    def __init__(self, folder):
        self.folder = folder

    def artifacts(self, step_id):
        return self.folder / "artifacts" / f"step_{int(step_id):02d}"


def test_errors_are_named(web):
    assert web.get("/api/runs/nothing").status_code == 404
    assert web.get("/api/runs/..%2Fetc").status_code == 404
    assert web.post("/api/runs", json={"idea": " "}).json()["detail"]["code"] == "no_idea"
    assert web.post("/api/runs", json={"idea": "x", "bogus": 1}).status_code == 422
    assert web.get("/api/health").json()["ok"]
    page = web.get("/")
    assert page.status_code == 200 and "no-store" in page.headers["cache-control"]


def test_pause_then_carry_on(web, script):
    searching, paused = threading.Event(), threading.Event()

    def research(prompt):
        searching.set()
        assert paused.wait(10)
        return FINDINGS

    script.add("DiscussorOutput", say("Researching.", ("research", "survey")), say("Here it is."))
    script.add("ResearcherOutput", research)
    script.add("OrchestratorOutput", decide("research", questions=["q"]), decide("ask_user", "ok?"))
    web.post("/api/runs", json={"idea": "idea", "name": "p"})
    assert searching.wait(10)
    assert web.post("/api/runs/p/pause").json()["as"] == "pausing"
    paused.set()
    d = settle(web, "p")
    assert d["stopped_midway"] and not d["waiting"]
    script.add("BootstrapAside", {"reply": "Yes; paused after the research."})
    assert web.post("/api/runs/p/message", json={"text": "still there?"}).status_code == 200, \
        "the discussor answers while the run is paused"
    d = settle(web, "p")
    assert d["transcript"][-1]["text"] == "Yes; paused after the research."
    assert web.post("/api/runs/p/resume").status_code == 200
    d = settle(web, "p")
    assert d["waiting"] and d["transcript"][-1]["text"] == "Here it is."
    assert web.post("/api/runs/p/resume").status_code == 400


def test_a_node_s_work_live_and_afterwards(web, script):
    """Click a graph node: its window shows the call under way -- the events
    and what is being written -- and every finished call, events and all."""
    reported, seen = threading.Event(), threading.Event()

    def research(prompt):
        script.report({"events": [
            {"kind": "command", "phase": "started", "command": "research_tools.py search lora", "at": 1.0},
            {"kind": "command", "phase": "completed", "command": "research_tools.py search lora",
             "exit_code": 0, "output": "12 papers", "at": 3.5},
            {"kind": "reasoning", "phase": "completed", "summary": ["Comparing ranks"], "at": 4.0}],
            "elapsed": 5.0, "idle": 1.0, "counts": {"command": 1},
            "live": {"message": '{"summary": "found'}, "last": "message: found"})
        reported.set()
        assert seen.wait(10)
        return FINDINGS

    script.add("DiscussorOutput", say("Researching.", ("research", "survey")), say("Done."))
    script.add("ResearcherOutput", research)
    script.add("OrchestratorOutput", decide("research", questions=["q"]), decide("ask_user", "ok?"))
    web.post("/api/runs", json={"idea": "LoRA rank", "name": "n"})
    assert reported.wait(10)
    live = web.get("/api/runs/n/live", params={"node": "researcher"}).json()["running"]
    assert len(live) == 1 and live[0]["session"] == "researcher-001" and live[0]["node"] == "researcher"
    assert [e["kind"] for e in live[0]["events"]] == ["command", "command", "reasoning"]
    assert live[0]["live"] == {"message": '{"summary": "found'} and live[0]["events_total"] == 3
    assert web.get("/api/runs/n/live", params={"node": "planner"}).json()["running"] == []
    seen.set()
    settle(web, "n")
    assert web.get("/api/runs/n/live").json()["running"] == [], "nothing runs any more"
    calls = web.get("/api/runs/n/activity", params={"node": "researcher"}).json()["calls"]
    assert len(calls) == 1 and calls[0]["session"] == "researcher-001" and calls[0]["turn"] == 1
    orch = web.get("/api/runs/n/activity", params={"node": "orchestrator"}).json()["calls"]
    assert [c["session"] for c in orch] == ["orchestrator-002", "orchestrator-001"], "newest first"
    disc = web.get("/api/runs/n/activity", params={"node": "discussor"}).json()["calls"]
    assert disc and all(c["node"] == "discussor" for c in disc)
    # A line on what went in and what came out of each call, for the list.
    assert all(c["said"] for c in disc) and disc[-1]["to"] in ("user", "orchestrator")
    assert orch[0]["to"] and orch[0]["tokens"]["input_tokens"] == 1000
    page = web.get("/node/n/researcher")
    assert page.status_code == 200 and 'id="call-tabs"' in page.text
    events = web.get("/api/runs/n/events?follow=false").text
    assert '"node": "researcher"' in events, "call events say which node"


def test_execute_json_written_before_the_start_chooses_models_and_keeps_the_start(web, script):
    folder = web.root / "early"
    S.Store(folder).write(approved_state())
    (folder / "execute.json").write_text(json.dumps({"roles": {"worker": {"provider": "fake",
                                                                          "model": "big"}}}))
    script.add("DiscussorOutput", say("Ready. Start the execution?"))
    script.add("OrchestratorOutput", decide("ask_user", "Start?"))
    web.post("/api/runs/early/message", json={"text": "hi"})
    d = settle(web, "early")
    assert d["stage"] == "bootstrap" and d["can_execute"], "the file alone starts nothing"
    from research_agent.execute import config as exec_config

    assert str(exec_config.load(folder).roles["worker"]) == "fake:big", "the file is read"
    script.add("ExecDecision", move("ask_user", task="Go?"))
    script.add("ExecDiscussorOutput", tell("Step 1 is next."))
    d = web.post("/api/runs/early/execute", json={}).json()
    assert d["stage"] == "execution"
    settle(web, "early")


def test_the_models_each_role_runs_on_can_be_changed(web, script, monkeypatch):
    folder = web.root / "m"
    S.Store(folder).write(approved_state())
    m = web.get("/api/runs/m/models").json()
    assert [g["name"] for g in m["groups"]] == ["bootstrap", "execution"]
    assert "discussor" in m["groups"][0]["roles"] and "worker" in m["groups"][1]["roles"]
    r = web.put("/api/runs/m/models", json={"group": "bootstrap", "role": "discussor",
                                            "provider": "codex", "model": "gpt-5.6-sol"})
    assert r.status_code == 200 and r.json()["now"] == "codex:gpt-5.6-sol"
    host = web.app_.state.hosts["m"]
    assert str(host.session.run.roles["discussor"]) == "codex:gpt-5.6-sol", "used from the next call"
    assert json.loads((folder / "config.json").read_text())["roles"]["discussor"]["provider"] == "codex"
    r = web.put("/api/runs/m/models", json={"group": "execution", "role": "worker",
                                            "provider": "claude_code", "model": "opus",
                                            "effort": "high"})
    assert r.status_code == 200 and json.loads((folder / "execute.json").read_text())[
        "roles"]["worker"] == {"provider": "claude_code", "model": "opus", "effort": "high",
                               "options": {"timeout": 1200}, "backup": []}
    assert web.get("/api/runs/m").json()["stage"] == "bootstrap", "execute.json starts nothing"
    # Backups: saved, kept when the model changes, and checked.
    r = web.put("/api/runs/m/models", json={"group": "bootstrap", "role": "discussor",
                                            "provider": "codex", "model": "gpt-5.6-sol",
                                            "backup": ["claude_code:sonnet", " "]})
    assert r.status_code == 200 and host.session.run.roles["discussor"].backup == (
        "claude_code:sonnet",)
    web.put("/api/runs/m/models", json={"group": "bootstrap", "role": "discussor",
                                        "provider": "codex", "model": ""})
    shown = web.get("/api/runs/m/models").json()["groups"][0]["roles"]["discussor"]
    assert shown["backup"] == ["claude_code:sonnet"] and shown["now"] == "codex"
    assert web.put("/api/runs/m/models", json={"group": "execution", "role": "worker",
                                               "provider": "codex",
                                               "backup": ["strata"]}).status_code == 400
    assert web.put("/api/runs/m/models", json={"group": "execution", "role": "chef",
                                               "provider": "codex"}).status_code == 400
    assert web.put("/api/runs/m/models", json={"group": "bootstrap", "role": "planner",
                                               "provider": "codex"}).status_code == 400
    monkeypatch.setattr(server, "_providers", lambda refresh=False: [{"provider": "codex", "models": ["m1"],
                                                        "efforts": ["high"], "error": ""}])
    assert web.get("/api/providers").json()[0]["models"] == ["m1"]


def test_a_failed_step_can_be_moved_on_from_the_page(web, script):
    folder = web.root / "s"
    st = approved_state()
    st, _ = S.apply(st, [{"op": "set", "section": "execution", "value": {"started": "2026-10-24T12:00:00+00:00", "steps": [
        {"id": "1", "title": "Data", "owner": "builder", "status": "stopped", "attempts": 3},
        {"id": "2", "title": "Train", "owner": "trainer"}]}}], by="x")
    S.Store(folder).write(st)
    (folder / "execute.json").write_text(json.dumps({"execute": {"confirm": "none"}}))
    web.app_.state.hosts.pop("s", None)
    script.add("ExecDecision", move("ask_user", task="Step 1 failed; what now?"))
    script.add("ExecDiscussorOutput", tell("Step 1 failed; another attempt or pass it?"))
    web.get("/api/runs/s")
    h = web.app_.state.hosts["s"]
    h._run(lambda on: h.session.start(on_step=on))
    d = settle(web, "s")
    assert d["stage"] == "execution" and d["waiting"]
    assert web.post("/api/runs/s/step", json={"step": "2", "action": "retry"}).status_code == 400
    assert web.post("/api/runs/s/step", json={"step": "1", "action": "waive"}).status_code == 400
    script.add("ExecDecision", move("retry", "1"), move("ask_user", task="?"))
    script.add("StepReport", done_writing(_Folder(folder), "1"))
    script.add("Verdict", verdict())
    script.add("ExecDiscussorOutput", tell("Step 1 passed."))
    r = web.post("/api/runs/s/step", json={"step": "1", "action": "retry"})
    assert r.status_code == 200 and r.json()["when"] == "now"
    settle(web, "s")
    rec = {x["id"]: x for x in S.Store(folder).read()["execution"]["steps"]}
    assert rec["1"]["status"] == "pass" and rec["1"]["attempts"] == 4 and rec["1"]["extra_attempts"] == 1


def test_the_page_passes_a_failed_step_and_reopens_a_skipped_one(web, script):
    # Run 20261028-1657: step 1 failed (it had done its part), step 2 was
    # skipped round it, and the discussor would neither pass nor reopen them.
    from tests.test_execute_graph import approved_state, decide as move, done_writing, \
        say as tell, verdict

    folder = web.root / "pr"
    st = approved_state()
    st, _ = S.apply(st, [{"op": "set", "section": "execution", "value": {
        "started": "2026-10-24T12:00:00+00:00", "steps": [
            {"id": "1", "title": "Data", "owner": "builder", "status": "fail", "attempts": 3},
            {"id": "2", "title": "Train", "owner": "trainer", "status": "skipped"}]}}], by="x")
    S.Store(folder).write(st)
    (folder / "execute.json").write_text(json.dumps({"execute": {"confirm": "none"}}))
    script.add("ExecDecision", move("ask_user", task="Step 1 failed; what now?"))
    script.add("ExecDiscussorOutput", tell("Step 1 failed and step 2 was skipped."))
    web.get("/api/runs/pr")
    h = web.app_.state.hosts["pr"]
    h._run(lambda on: h.session.start(on_step=on))
    settle(web, "pr")
    script.add("ExecDecision", move("ask_user", task="Step 2?"))
    script.add("ExecDiscussorOutput", tell("Step 1 passes on your decision."))
    r = web.post("/api/runs/pr/step", json={"step": "1", "action": "waive",
                                            "why": "its own check holds"})
    assert r.status_code == 200
    settle(web, "pr")
    script.add("ExecDecision", move("retry", "2"), move("ask_user", task="?"))
    script.add("StepReport", done_writing(_Folder(folder), "2"))
    script.add("Verdict", verdict())
    script.add("ExecDiscussorOutput", tell("Step 2 passed."))
    assert web.post("/api/runs/pr/step", json={"step": "2", "action": "retry"}).status_code == 200
    settle(web, "pr")
    rec = {x["id"]: x for x in S.Store(folder).read()["execution"]["steps"]}
    assert rec["1"]["status"] == "pass" and rec["2"]["status"] == "pass"
    # And back: rewind to step 1 -- both start over, the team goes on.
    script.add("ExecDecision", move("ask_user", task="Start over: run 1?"))
    script.add("ExecDiscussorOutput", tell("Steps 1 and 2 start over. Run step 1?"))
    assert web.post("/api/runs/pr/step", json={"step": "1", "action": "rewind"}).status_code == 200
    settle(web, "pr")
    rec = {x["id"]: x for x in S.Store(folder).read()["execution"]["steps"]}
    assert rec["1"]["status"] == rec["2"]["status"] == "pending"
    assert web.post("/api/runs/pr/step", json={"step": "1", "action": "rewind"}).status_code == 400


def test_a_node_s_settings_and_what_a_running_call_is_sent(web, script):
    folder = web.root / "ni"
    S.Store(folder).write(approved_state())
    web.get("/api/runs/ni")
    info = web.get("/api/runs/ni/nodes/designer").json()
    assert info["role"] == "designer" and info["runs_on"]["provider"] == "fake"
    assert "You are the DESIGNER" in info["instructions"]
    assert web.get("/api/runs/ni/nodes/planner").status_code == 400
    trainer = web.get("/api/runs/ni/nodes/trainer").json()   # from the design's preview
    assert trainer["role"] == "worker" and trainer["steps"] == ["2"] and trainer["access"] == "full"
    assert "Train with seeds 17 and 29." in trainer["design"]
    assert web.get("/api/runs/ni/nodes/nobody").status_code == 400

    started = threading.Event()
    seen = threading.Event()

    def asked(prompt):
        started.set()
        assert seen.wait(10)
        return say("Hello.")

    script.add("DiscussorOutput", asked)
    web.post("/api/runs/ni/message", json={"text": "What is the plan?"})
    assert started.wait(10)
    running = web.get("/api/runs/ni/live", params={"node": "discussor"}).json()["running"]
    assert "=== THE USER SAYS ===\nWhat is the plan?" in running[0]["prompt"]
    seen.set()
    settle(web, "ni")
    events = web.get("/api/runs/ni/events?follow=false").text
    assert "THE USER SAYS" not in events, "prompts are not sent down the event stream"
    disc = web.get("/api/runs/ni/nodes/discussor").json()
    assert disc["role"] == "discussor" and disc["group"] == "bootstrap"


def test_a_node_s_skills_can_be_chosen(web, script, tmp_path):
    folder = web.root / "sk"
    S.Store(folder).write(approved_state())
    names = {s["name"] for s in web.get("/api/runs/sk/skills").json()["skills"]}
    assert {"research", "peer-review", "experiment-design"} <= names
    designer = web.get("/api/runs/sk/nodes/designer").json()["slots"]
    assert [s["key"] for s in designer] == ["designer"]
    r = web.put("/api/runs/sk/nodes/designer/skills", json={"key": "designer", "mode": "chosen",
                                                            "skills": ["experiment-design", "statistical-power"]})
    assert r.status_code == 200 and r.json()["skills"] == ["experiment-design", "statistical-power"]
    saved = json.loads((folder / "config.json").read_text())["skills"]
    assert saved["designer"] == ["experiment-design", "statistical-power"]
    host = web.app_.state.hosts["sk"]
    assert host.session.run.skills["designer"] == ["experiment-design", "statistical-power"]
    assert web.put("/api/runs/sk/nodes/designer/skills", json={"key": "designer", "mode": "chosen",
                                                               "skills": ["nope"]}).status_code == 400
    assert web.put("/api/runs/sk/nodes/discussor/skills",
                   json={"key": "discussor", "mode": "none"}).json()["mode"] == "none"
    # An execution node: the design's skills, or the user's choice over them.
    trainer = web.get("/api/runs/sk/nodes/trainer").json()["slots"][0]
    assert trainer["from_design"] and trainer["group"] == "execution"
    r = web.put("/api/runs/sk/nodes/trainer/skills", json={"key": "trainer", "mode": "auto"})
    assert r.json()["mode"] == "auto" and not r.json()["from_design"]
    assert json.loads((folder / "execute.json").read_text())["skills"]["nodes"] == {"trainer": "auto"}
    r = web.put("/api/runs/sk/nodes/trainer/skills", json={"key": "trainer", "mode": "design"})
    assert r.json()["from_design"]


def test_an_execution_node_s_skills_override_the_design(tmp_path):
    from research_agent.execute import config as exec_config, nodes as exec_nodes
    from research_agent.bootstrap.config import RoleConfig as RC

    cfg = replace(exec_config.defaults(), node_skills={"trainer": ["peft"], "validator": None})
    run = exec_nodes.ExecRun(tmp_path, {r: RC("fake") for r in exec_config.ROLES}, skills_dir=cfg.skills_dir,
                             skills={}, limits=cfg)
    assert exec_nodes.skills_for(run, {"name": "trainer", "skills": ["transformers"]}) == ["peft"]
    assert exec_nodes.skills_for(run, {"name": "checker", "skills": ["peft"]}, "validator") is None
    assert exec_nodes.skills_for(run, {"name": "builder", "skills": ["peft", "nope"]}) == ["peft"]


def test_a_call_in_a_line_what_went_in_and_what_came_out():
    from research_agent.web.host import previews, sections

    brief = "=== THE USER SAYS ===\nyes, go\n\n=== APPROVALS ===\nplan: v2"
    assert sections(brief) == [("THE USER SAYS", "yes, go"), ("APPROVALS", "plan: v2")]
    assert previews(brief, json.dumps({"reply": "On it.", "hand_to": "orchestrator"})) == {
        "said": "you: yes, go", "outcome": "On it.", "to": "orchestrator"}
    aside = "=== AN ASIDE -- the team is working ===\n...\n=== THE USER SAYS ===\nwhy H2?"
    assert previews(aside, "{}")["said"] == "you (aside): why H2?"
    done = previews("=== THE LAST PIECE OF WORK ===\nplanner wrote H1-H3",
                    json.dumps({"action": "run", "step": "4", "reason": "deps pass"}))
    assert done == {"said": "planner wrote H1-H3", "outcome": "deps pass", "to": "run 4"}
    assert previews("Your answer could not be used:\n- no task\n\nAgain.", "x")["said"] == "retry: no task"
    assert previews("plan it", json.dumps({"summary": "", "hypotheses": [1, 2]}))["outcome"] == "2 hypotheses"


def test_the_researchers_sources_can_be_chosen(web, monkeypatch):
    folder = web.root / "src"
    S.Store(folder).write(S.new())
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    r = web.get("/api/runs/src/research").json()
    assert r["sources"] == ["openalex", "s2"]
    scholar = next(a for a in r["available"] if a["name"] == "scholar")
    assert scholar["key"] == "SERPAPI_API_KEY" and scholar["keyed"] is False
    r = web.put("/api/runs/src/research", json={"sources": ["pubmed", "openalex"]})
    assert r.status_code == 200 and r.json()["sources"] == ["openalex", "pubmed"]
    assert json.loads((folder / "config.json").read_text())["research"]["sources"] == ["openalex", "pubmed"]
    assert json.loads((folder / ".research_sources").read_text()) == {"sources": ["openalex", "pubmed"]}
    assert web.put("/api/runs/src/research", json={"sources": []}).status_code == 400
    assert web.put("/api/runs/src/research", json={"sources": ["bing"]}).status_code == 400


def test_autopilot_from_the_page_to_the_end(web, script):
    from research_agent import autopilot

    folder = web.root / "auto"
    st = approved_state()
    st["approvals"] = {k: v for k, v in st["approvals"].items() if k != "design"}
    S.Store(folder).write(st)
    script.add("DiscussorOutput", say("The design is ready. Approve?"), say("Approved. Done."),
               say("The design is approved: execution next."))
    script.add("OrchestratorOutput", decide("ask_user", "Approve the design?"))
    web.post("/api/runs/auto/message", json={"text": "hello"})
    d = settle(web, "auto")
    assert d["waiting"] and not d["autopilot"]["on"]

    # Unattended means its limits are set first.
    r = web.put("/api/runs/auto/autopilot", json={"on": True, "wall_hours": 0, "gpu_hours": 0})
    assert r.status_code == 400 and "limits" in r.json()["detail"]["message"]

    def answer(stage):
        def it(prompt):
            return {"answer": "Approve the design." if stage == "boot" else "Go.",
                    "why": "it is what the user asked for", "approve": ["design"] if stage == "boot"
                    else [], "go": stage == "exec"}
        return it
    script.add("AutopilotOutput", answer("boot"), answer("exec"))
    script.add("ExecDecision", move("run", "1"), move("run", "2"), move("run", "2"), move("done"))
    script.add("StepReport", done_writing(_Folder(folder), "1"), done_writing(_Folder(folder), "2"))
    script.add("Verdict", verdict(), verdict())
    script.add("PaperReport", PAPER)
    reviewed(script)
    script.add("ExecDiscussorOutput", tell("Step 2 waits for your go."), tell("Going ahead."),
               tell("All done; 5/10."))
    r = web.put("/api/runs/auto/autopilot", json={"on": True, "max_decisions": 5,
                                                   "wall_hours": 2, "gpu_hours": 10})
    assert r.status_code == 200 and r.json()["on"] and r.json()["gpu_hours"] == 10
    # It answers the waiting run, approves, and starts the execution itself.
    end = time.time() + 15
    while time.time() < end:
        d = web.get("/api/runs/auto").json()
        if d["stage"] == "execution" and d["outcome"] == "done" and not d["busy"]:
            break
        time.sleep(0.05)
    assert d["stage"] == "execution" and d["outcome"] == "done"
    st = S.Store(folder).read()
    assert S.approved(st, "design")
    assert [x["by"] for x in st["decisions"]].count("autopilot") == 2
    exe = json.loads((folder / "execute.json").read_text())["execute"]
    assert (exe["wall_hours"], exe["gpu_hours"]) == (2, 10), "the limits it runs under"
    kinds = [m["kind"] for m in d["transcript"] if m["role"] == "user"]
    assert "autopilot" in kinds
    assert autopilot.load(folder).decisions == 2
    assert web.put("/api/runs/auto/autopilot", json={"on": False}).json()["on"] is False


def test_a_run_set_up_before_it_starts(web, script):
    r = web.post("/api/runs", json={"idea": "Few-shot selection by search", "name": "held",
                                    "start": False})
    d = r.json()
    assert r.status_code == 200 and d["held_idea"] == "Few-shot selection by search"
    assert not d["started"] and not d["busy"] and script.calls == [], "no model is called yet"
    assert web.put("/api/runs/held/models", json={"group": "bootstrap", "role": "discussor",
                                                  "provider": "fake"}).status_code == 200
    script.add("DiscussorOutput", say("Interesting. What data?"))
    assert web.post("/api/runs/held/start").status_code == 200
    d = settle(web, "held")
    assert d["waiting"] and not d["held_idea"]
    assert "Few-shot selection by search" in script.calls[0]["prompt"]
    assert web.post("/api/runs/held/start").status_code == 400, "started already"


def test_a_new_model_takes_over_a_call_that_waits(web, script, monkeypatch):
    from research_backend.backends._progress import notice, wait_or_cancel

    class Broke:
        name, max_access, on_progress = "codex", script.max_access, None

        def run_structured(self, **kw):
            notice(self, "Out of credits. Waiting 30 min.", until=time.time() + 1800)
            wait_or_cancel(1800, None, None)
            raise AssertionError("the wait should have ended")

    broke = Broke()
    monkeypatch.setattr("research_backend.conversation._backend",
                        lambda provider, *a, **k: broke if provider == "claude_code" else script)
    web.post("/api/runs", json={"idea": "an idea", "name": "sw", "start": False})
    web.put("/api/runs/sw/models", json={"group": "bootstrap", "role": "discussor",
                                         "provider": "claude_code"})
    script.add("DiscussorOutput", say("Hello from the new model."))
    web.post("/api/runs/sw/start")
    end = time.time() + 5
    while time.time() < end and not any(c.get("notice") for c in
                                        web.get("/api/runs/sw/live").json()["running"]):
        time.sleep(0.05)
    assert web.get("/api/runs/sw/live").json()["running"][0]["notice"].startswith("Out of credits")
    # The user picks a model with credits: the wait ends, and the run goes on with it.
    web.put("/api/runs/sw/models", json={"group": "bootstrap", "role": "discussor",
                                         "provider": "fake"})
    end = time.time() + 10
    while time.time() < end:
        d = web.get("/api/runs/sw").json()
        if d["waiting"] and not d["busy"]:
            break
        time.sleep(0.05)
    assert d["waiting"] and d["transcript"][-1]["text"] == "Hello from the new model."
    assert web.get("/api/runs/sw/live").json()["running"] == [], "no stale call left"


def test_autopilot_carries_on_after_an_error(web, script):
    from research_agent import autopilot

    def boom(prompt):
        raise RuntimeError("the server fell over")

    web.post("/api/runs", json={"idea": "an idea", "name": "err", "start": False})
    web.put("/api/runs/err/autopilot", json={"on": True, "max_decisions": 2,
                                              "wall_hours": 1, "gpu_hours": 1})
    script.add("DiscussorOutput", boom, say("Hello. Which data?"), say("Fine. Anything else?"))
    script.add("AutopilotOutput", {"answer": "Use SST-2.", "why": "the user named it"})
    web.post("/api/runs/err/start")
    end = time.time() + 10
    while time.time() < end:
        d = web.get("/api/runs/err").json()
        if d["waiting"] and not d["busy"]:
            break
        time.sleep(0.05)
    assert d["waiting"] and d["transcript"][-1]["text"] == "Fine. Anything else?"
    s = autopilot.load(web.root / "err")
    assert s.decisions == 2 and s.handed_back, "the restart counted as a decision; then it handed back"
    events = web.get("/api/runs/err/events?follow=false").text
    assert "Autopilot: the run stopped with an error" in events


def test_a_run_can_be_deleted_to_the_trash(web, monkeypatch):
    from research_agent.execute import jobs as J

    S.Store(web.root / "old").write(S.new())
    web.get("/api/runs/old")                                   # open: it is closed first
    monkeypatch.setattr(J, "records", lambda folder: [{"id": "j1"}])
    monkeypatch.setattr(J, "status", lambda job: {"state": "running"})
    r = web.post("/api/runs/old/trash")
    assert r.status_code == 409 and "Jobs still run (j1)" in r.text
    monkeypatch.setattr(J, "status", lambda job: {"state": "finished"})
    r = web.post("/api/runs/old/trash")
    assert r.status_code == 200
    moved = Path(r.json()["moved_to"])
    assert moved.parent == web.root / ".trash" and (moved / "state" / "research.json").exists()
    assert not (web.root / "old").exists()
    assert "old" not in [x["id"] for x in web.get("/api/runs").json()]
    assert web.post("/api/runs/old/trash").status_code == 404
    assert web.post("/api/runs/.trash/trash").status_code == 404


def test_the_paper_window_shows_the_writer_s_pdf_first(web):
    folder = web.root / "paper"
    st = S.new()
    st["design"] = {"nodes": [{"name": "w", "role": "writer", "access": "write"},
                            {"name": "b", "role": "worker", "access": "write"}]}
    st["execution"] = {"steps": [{"id": "1", "owner": "b", "artifacts": ["artifacts/plot.pdf"]},
                                 {"id": "2", "owner": "w", "artifacts": ["../artifacts/paper.pdf"]}]}
    (folder / "state").mkdir(parents=True)      # only what the papers need
    (folder / "state" / "research.json").write_text(json.dumps(st))
    (folder / "artifacts").mkdir()
    (folder / "artifacts" / "paper.pdf").write_bytes(b"%PDF-1.4 paper")
    (folder / "artifacts" / "plot.pdf").write_bytes(b"%PDF-1.4 plot")
    (folder / "research" / "papers").mkdir(parents=True)
    (folder / "research" / "papers" / "2410.01495.pdf").write_bytes(b"%PDF-1.4 reference")
    (folder / "rb").mkdir()
    (folder / "rb" / "x.pdf").write_bytes(b"%PDF-1.4")
    papers = web.get("/api/runs/paper/papers").json()["papers"]
    assert [(p["path"], p["step"]) for p in papers] == [("artifacts/paper.pdf", "2"),
                                                       ("artifacts/plot.pdf", "")]
    r = web.get("/api/runs/paper/raw", params={"path": "artifacts/paper.pdf"})
    assert r.status_code == 200 and r.content == b"%PDF-1.4 paper"
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")
    assert web.get("/api/runs/paper/raw", params={"path": "state/research.json"}).status_code == 415
    assert web.get("/api/runs/paper/raw", params={"path": "../../etc/passwd"}).status_code == 404
    assert web.get("/paper/paper").status_code == 200
    assert web.get("/review/paper").status_code == 200
