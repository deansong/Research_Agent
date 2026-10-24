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

import pytest
from fastapi.testclient import TestClient

from research_agent import state as S
from research_agent.web import server
from tests.scripted import script  # noqa: F401 -- fixture
from tests.test_execute_graph import REVIEW, approved_state, done_writing, verdict
from tests.test_execute_graph import decide as move, say as tell
from tests.test_graph import FINDINGS, decide, say


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
    assert st["approved"] == {s: False for s in S.APPROVABLE}
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
    script.add("ReviewOutput", REVIEW)
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
    page = web.get("/node/n/researcher")
    assert page.status_code == 200 and "node live view" in page.text
    events = web.get("/api/runs/n/events?follow=false").text
    assert '"node": "researcher"' in events, "call events say which node"


def test_execute_json_written_before_the_start_chooses_models_and_keeps_the_start(web, script):
    folder = web.root / "early"
    S.Store(folder).write(approved_state())
    (folder / "execute.json").write_text(json.dumps({"roles": {"runner": {"provider": "fake",
                                                                          "model": "big"}}}))
    script.add("DiscussorOutput", say("Ready. Start the execution?"))
    script.add("OrchestratorOutput", decide("ask_user", "Start?"))
    web.post("/api/runs/early/message", json={"text": "hi"})
    d = settle(web, "early")
    assert d["stage"] == "bootstrap" and d["can_execute"], "the file alone starts nothing"
    from research_agent.execute import config as exec_config

    assert str(exec_config.load(folder).roles["runner"]) == "fake:big", "the file is read"
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
    assert "discussor" in m["groups"][0]["roles"] and "runner" in m["groups"][1]["roles"]
    r = web.put("/api/runs/m/models", json={"group": "bootstrap", "role": "discussor",
                                            "provider": "codex", "model": "gpt-5.6-sol"})
    assert r.status_code == 200 and r.json()["now"] == "codex:gpt-5.6-sol"
    host = web.app_.state.hosts["m"]
    assert str(host.session.run.roles["discussor"]) == "codex:gpt-5.6-sol", "used from the next call"
    assert json.loads((folder / "config.json").read_text())["roles"]["discussor"]["provider"] == "codex"
    r = web.put("/api/runs/m/models", json={"group": "execution", "role": "runner",
                                            "provider": "claude_code", "model": "opus",
                                            "effort": "high"})
    assert r.status_code == 200 and json.loads((folder / "execute.json").read_text())[
        "roles"]["runner"] == {"provider": "claude_code", "model": "opus", "effort": "high",
                               "options": {"timeout": 1200}}
    assert web.get("/api/runs/m").json()["stage"] == "bootstrap", "execute.json starts nothing"
    assert web.put("/api/runs/m/models", json={"group": "execution", "role": "chef",
                                               "provider": "codex"}).status_code == 400
    assert web.put("/api/runs/m/models", json={"group": "bootstrap", "role": "planner",
                                               "provider": "gpt"}).status_code == 400
    monkeypatch.setattr(server, "_providers", lambda: [{"provider": "codex", "models": ["m1"],
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
