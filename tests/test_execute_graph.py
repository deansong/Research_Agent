"""
WHAT:  The execution stage on scripted answers: a whole run from the approved
       design to done (with a real background job), what code refuses the
       orchestrator, retries with the verdict, skipping, the budget, and
       resuming while jobs run.
WHY:   The orchestrator chooses; what code guarantees -- dependencies,
       attempts, the user's go, the budget, that only the validator passes a
       step, who sees what -- is what has to be pinned.

Run with:   python -m pytest tests/test_execute_graph.py -q
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from research_agent import state as S
from research_agent.bootstrap.config import RoleConfig
from research_agent.execute import config, jobs, nodes
from research_agent.execute.session import Execution
from tests.scripted import script  # noqa: F401 -- fixture

PLAN = {"summary": "two steps", "steps": [
    {"id": "1", "title": "Data", "detail": "count rows", "check": "rows.json exists",
     "rubric": ["R1"]},
    {"id": "2", "title": "Train", "detail": "two seeds", "check": "scores for both seeds",
     "depends_on": ["1"], "gate": True, "rubric": ["R1"]}]}
DESIGN = {"summary": "four nodes", "nodes": [
    {"name": "lead", "role": "orchestrator", "instructions": "Run the plan in order."},
    {"name": "builder", "role": "worker", "steps": ["1"], "access": "write",
     "instructions": "Prepare the data.", "validated_by": ["R1"]},
    {"name": "trainer", "role": "runner", "steps": ["2"], "access": "write",
     "instructions": "Train with seeds 17 and 29.", "validated_by": ["R1"]},
    {"name": "checker", "role": "validator", "access": "read_only", "instructions": "Check."}]}


def approved_state(design=DESIGN, plan=PLAN) -> dict:
    st, _ = S.apply(S.new(), [
        {"op": "merge", "section": "goal", "value": {"question": "Does rank matter?"}},
        {"op": "add", "section": "hypotheses", "value": {"statement": "Rank matters",
                                                         "prediction": "r=64 wins"}},
        {"op": "add", "section": "rubric", "value": {"criterion": "Seeds fixed"}},
        {"op": "set", "section": "protocol", "value": {"seeds": "17, 29"}},
        {"op": "set", "section": "plan", "value": plan},
        {"op": "set", "section": "design", "value": design}], by="bootstrap")
    st, _ = S.apply(st, [{"op": "approve", "section": s} for s in S.APPROVABLE], by="user")
    return st


@pytest.fixture
def run(tmp_path):
    folder = tmp_path / "run"
    S.Store(folder).write(approved_state())
    limits = replace(config.defaults(), poll_seconds=0.05)
    roles = {r: RoleConfig("fake") for r in (*config.ROLES, "discussor")}
    return nodes.ExecRun(folder, roles, skills_dir=None, skills={}, limits=limits, quiet=True)


def decide(action, step="", task="", **fields):
    return {"action": action, "step": step, "reason": f"because {action} {step}".strip(),
            "task": task, "summary": f"after {action} {step}".strip(), **fields}


def say(reply, *requests, hand_to=None, **fields):
    hand = hand_to or ("orchestrator" if requests or fields.get("go") else "user")
    return {"reply": reply, "hand_to": hand, "requests": list(requests), **fields}


def done_writing(run, step_id, name="out.txt", text="ok"):
    """An owner's answer that writes its artifact first, as a real one would."""
    def answer(prompt):
        folder = run.artifacts(step_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text)
        return {"state": "done", "summary": f"step {step_id} written", "artifacts": [name]}
    return answer


REVIEW = {"summary": "A small, clean study.", "strengths": ["fixed seeds"],
          "weaknesses": ["two seeds only"], "questions": ["why these ranks?"],
          "limitations": "one dataset", "soundness": 3, "presentation": 3, "contribution": 2,
          "rating": 5, "confidence": 4, "requests": [
              {"what": "Run five seeds", "why": "two give no variance", "priority": "must"}]}


def verdict(v="pass", **fields):
    return {"verdict": v, "summary": f"{v} it is", **fields}


def test_a_whole_execution_from_the_approved_design_to_done(run, script):
    def launch(prompt):
        assert "launch --step 2" in prompt, "the brief shows how to launch a job"
        job = jobs.launch(run.folder, "2", "echo trained > scores.txt", cwd=run.artifacts("2"))
        return {"state": "launched", "summary": "two seeds launched", "jobs": [job["id"]]}

    script.add("ExecDecision", decide("run", "1"), decide("run", "2"), decide("run", "2"),
               decide("done"))
    script.add("ReviewOutput", REVIEW)
    script.add("StepReport", done_writing(run, "1", "rows.json", "[1000]"), launch,
               lambda prompt: {"state": "done", "summary": "both seeds scored",
                               "artifacts": ["scores.txt"], "metrics": [
                                   {"name": "F1 seed 17", "value": "0.81"}]} if
               "YOUR JOBS FINISHED" in prompt and "[finished, exit 0]" in prompt else
               {"state": "failed", "summary": "unexpected"})
    script.add("Verdict", verdict(), verdict())
    script.add("ExecDiscussorOutput", say("Step 1 passed. Step 2 trains 2 seeds; go?"),
               say("Starting step 2.", go=True), say("All steps passed."))

    ex = Execution(run)
    seen = []
    turn = ex.start(on_step=lambda node, update: seen.append(node))
    assert turn.reply == "Step 1 passed. Step 2 trains 2 seeds; go?" and turn.waiting
    assert seen == ["orchestrator", "builder", "validator", "orchestrator", "discussor"]
    assert ex.awaiting == "2", "step 2 is gated: the user says go first"
    st = ex.research
    assert nodes.records(st)["1"]["status"] == "pass" and st["status"]["phase"] == "executing"

    turn = ex.answer("yes, go ahead")
    assert turn.outcome == "done" and turn.reply == "All steps passed."
    st = ex.research
    rec = nodes.records(st)
    assert rec["2"]["status"] == "pass" and rec["2"]["go"] and rec["2"]["jobs"] == ["s2-001"]
    assert rec["2"]["artifacts"] == ["scores.txt"] and st["status"]["phase"] == "done"
    assert any(d["what"] == "Go for step 2: Train" and d["by"] == "user" for d in st["decisions"])
    assert "2. [pass] Train (trainer, go given, 1 attempts" in S.render(st, ["execution"])
    assert (run.artifacts("2") / "scores.txt").read_text().strip() == "trained"

    # The owner of step 2 was woken in its own conversation; validators are fresh.
    owners = [c for c in script.calls if c["schema"] == "StepReport"]
    assert owners[1]["thread"] is None and owners[2]["thread"] is not None
    assert all(c["thread"] is None for c in script.calls if c["schema"] in ("Verdict",
                                                                            "ExecDecision"))
    assert all("yes, go ahead" not in c["prompt"] for c in script.calls
               if c["schema"] != "ExecDiscussorOutput"), "the user's words reach only the discussor"
    sessions = sorted(p.name for p in (run.folder / "rb" / "sessions").iterdir())
    assert {"step01-a1", "step02-a1", "validate01-a1-001", "validate02-a1-001",
            "discussor"} <= set(sessions)
    # Owners run commands: research_backend's full access; the validator reads.
    access = {c["schema"]: c["access"].value for c in script.calls}
    assert access["StepReport"] == "full" and access["Verdict"] == "read_only"
    # Each owner turn's code is committed.
    log = subprocess.run(["git", "log", "--format=%s"], cwd=run.work, capture_output=True,
                         text=True).stdout
    assert "step 2, attempt 1: done" in log and "step 1, attempt 1: done" in log
    ex.close()


def test_the_orchestrator_can_only_make_open_moves(run, script):
    script.add("ExecDecision", decide("run", "2"), decide("done"), decide("ask_user",
                                                                          task="Which GPU?"))
    script.add("ExecDiscussorOutput", say("Which GPU should I use?"))
    Execution(run).start()
    first, second, third = script.prompts("ExecDecision")
    assert "- run 1\n" in first and "run 2" not in first.split("OPEN MOVES")[1].split("===")[1]
    assert "run 2 is not open now; open moves: run 1" in second
    assert "done: not every step has passed or been skipped" in third
    assert not script.prompts("StepReport")


def test_a_failed_verdict_is_retried_with_its_advice_until_attempts_run_out(run, script,
                                                                             monkeypatch):
    run.limits = replace(run.limits, max_attempts=2)
    script.add("ExecDecision", decide("run", "1"), decide("retry", "1", "use the full split"),
               decide("stop", task="step 1 cannot pass"))
    script.add("StepReport", done_writing(run, "1"), done_writing(run, "1"))
    script.add("Verdict", verdict("fail", problems=["only 900 rows"], advice="use all rows"),
               verdict("fail", problems=["still 900"], advice="ask the user"))
    script.add("ExecDiscussorOutput", say("Step 1 failed twice; stopped."))
    turn = Execution(run).start()
    assert turn.outcome == "stopped"
    second_owner = script.prompts("StepReport")[1]
    assert "THE LAST ATTEMPT FAILED" in second_owner and "only 900 rows" in second_owner
    assert "advice: use all rows" in second_owner and "use the full split" in second_owner
    third = script.prompts("ExecDecision")[2]
    assert "retry 1" not in third.split("OPEN MOVES")[1].split("===")[1], "no attempts left"
    rec = nodes.records(Execution(run).research)["1"]
    assert rec["status"] == "fail" and rec["attempts"] == 2
    assert rec["sessions"] == ["step01-a1", "step01-a2"]


def test_a_skipped_step_leaves_a_record_and_unblocks_the_next(tmp_path, script):
    plan = {"steps": [PLAN["steps"][0], {**PLAN["steps"][1], "gate": False}]}
    folder = tmp_path / "run"
    S.Store(folder).write(approved_state(plan=plan))
    run = nodes.ExecRun(folder, {r: RoleConfig("fake") for r in (*config.ROLES, "discussor")},
                        skills={}, limits=replace(config.defaults(), poll_seconds=0.05),
                        quiet=True)
    script.add("ExecDecision", decide("skip", "1", "the protocol's T3 branch does not apply"),
               decide("run", "2"), decide("done"))
    script.add("ReviewOutput", REVIEW)
    script.add("StepReport", done_writing(run, "2"))
    script.add("Verdict", verdict())
    script.add("ExecDiscussorOutput", say("Done; step 1 was skipped."))
    turn = Execution(run).start()
    assert turn.outcome == "done"
    assert "T3 branch does not apply" in (run.artifacts("1") / "SKIPPED.md").read_text()
    assert nodes.records(Execution(run).research)["1"]["status"] == "skipped"


def test_going_over_the_budget_stops_the_jobs_and_asks_the_user(run, script):
    run.limits = replace(run.limits, gpu_hours=1.0)
    job = jobs.launch(run.folder, "1", "sleep 30")
    record = jobs.get(run.folder, job["id"])
    record["gpus"], record["started"] = [0, 1], record["started"] - 3600
    (run.folder / "jobs" / f"{job['id']}.json").write_text(__import__("json").dumps(record))
    script.add("ExecDiscussorOutput", say("We hit the GPU-hour limit."))
    turn = Execution(run).start()
    assert turn.outcome == "stopped" and not script.prompts("ExecDecision")
    told = script.prompts("ExecDiscussorOutput")[0]
    assert "the limit of 1 GPU-hours is used up" in told
    assert jobs.status(jobs.get(run.folder, job["id"]))["state"] != "running"


def test_a_run_resumes_while_its_jobs_run(run, script):
    def launch(prompt):
        job = jobs.launch(run.folder, "1", "sleep 0.5; echo done > rows.json",
                          cwd=run.artifacts("1"))
        return {"state": "launched", "summary": "counting", "jobs": [job["id"]]}

    script.add("ExecDecision", decide("run", "1"), decide("ask_user", task="Go on to step 2?"))
    script.add("StepReport", launch, {"state": "done", "summary": "counted",
                                      "artifacts": ["rows.json"]})
    script.add("Verdict", verdict())
    script.add("ExecDiscussorOutput", say("Step 1 passed. Go on?"))

    class Leave(Exception):
        pass

    def leave_after_launch(node, update):
        if node == "builder":
            raise Leave

    with pytest.raises(Leave):
        Execution(run).start(on_step=leave_after_launch)
    again = Execution(run)
    assert again.started and nodes.records(again.research)["1"]["status"] == "waiting"
    turn = again.resume()
    assert turn.reply == "Step 1 passed. Go on?"
    assert nodes.records(again.research)["1"]["status"] == "pass"


def test_a_design_that_cannot_be_run_is_refused(tmp_path):
    bad = {"nodes": [n for n in DESIGN["nodes"] if n["name"] != "trainer"]}
    folder = tmp_path / "run"
    S.Store(folder).write(approved_state(design=bad))
    run = nodes.ExecRun(folder, {r: RoleConfig("fake") for r in (*config.ROLES, "discussor")},
                        skills={})
    with pytest.raises(ValueError, match="plan step 2 has no node"):
        Execution(run)
    st = S.Store(folder).read()
    st, _ = S.apply(st, [{"op": "merge", "section": "plan", "value": {"summary": "changed"}}],
                    by="x")
    S.Store(folder).write(st)
    with pytest.raises(ValueError, match="the plan is not approved"):
        Execution(run)


def test_an_owner_must_show_its_files_and_its_jobs(run, script):
    script.add("ExecDecision", decide("run", "1"), decide("ask_user", task="?"))
    script.add("StepReport",
               {"state": "done", "summary": "s", "artifacts": ["missing.json"]},
               {"state": "launched", "summary": "s", "jobs": ["s9-001"]},
               done_writing(run, "1"))
    script.add("Verdict", verdict())
    script.add("ExecDiscussorOutput", say("ok"))
    Execution(run).start()
    retries = script.prompts("StepReport")[1:]
    assert "artifact missing.json does not exist" in retries[0]
    assert "there is no job s9-001" in retries[1]


def test_the_user_can_stop_and_the_discussor_never_relays_into_more_work(run, script):
    script.add("ExecDecision", decide("ask_user", task="Use GPUs 0-3?"))
    script.add("ExecDiscussorOutput", say("Use GPUs 0-3?", "run step 1", hand_to="user"),
               say("Stopped."))
    ex = Execution(run)
    turn = ex.start()
    assert turn.waiting and len(script.prompts("ExecDecision")) == 1
    turn = ex.answer(stop=True)
    assert turn.outcome == "stopped" and ex.research["status"]["phase"] == "stopped"
    assert "(I stopped the execution.)" in script.prompts("ExecDiscussorOutput")[1]
    assert Path(run.folder / "state" / "research.md").read_text().count("## Execution") == 1


def test_a_go_by_command_reaches_the_discussor_and_a_result_keeps_the_approval(run, script):
    script.add("ExecDecision", decide("run", "1"), decide("run", "2"), decide("run", "2"),
               decide("done"))
    script.add("ReviewOutput", REVIEW)
    script.add("StepReport", done_writing(run, "1"), lambda prompt: {
        **done_writing(run, "2")(prompt),
        "hypotheses": [{"id": "H1", "status": "supported", "why": "out.txt"}]})
    script.add("Verdict", verdict(), verdict())
    script.add("ExecDiscussorOutput", say("Step 2 next; go?"), say("Done."))
    st = S.Store(run.folder).read()
    st["design"]["nodes"][2]["role"] = "writer"
    S.Store(run.folder).write(st)
    ex = Execution(run)
    ex.start()
    turn = ex.answer(go=True)
    assert turn.outcome == "done"
    told = script.prompts("ExecDiscussorOutput")[1]
    assert "Go for step 2: Train" in told, "the discussor learns of a /go"
    st = ex.research
    assert st["hypotheses"][0]["status"] == "supported" and S.approved(st, "hypotheses")


def test_the_review_is_kept_for_the_next_run(run, script):
    script.add("ExecDecision", decide("run", "1"), decide("run", "2"), decide("run", "2"),
               decide("done"))
    script.add("StepReport", done_writing(run, "1"), done_writing(run, "2"))
    script.add("Verdict", verdict(), verdict())
    script.add("ReviewOutput", REVIEW)
    script.add("ExecDiscussorOutput", say("Go?"), say("Reviewed: 5/10."))
    ex = Execution(run)
    ex.start()
    turn = ex.answer(go=True)
    assert turn.outcome == "done" and turn.reply == "Reviewed: 5/10."
    review = ex.research["reviews"][0]
    assert review["id"] == "V1" and review["rating"] == 5 and review["run"] == str(run.folder)
    assert review["requests"][0] == {"what": "Run five seeds", "why": "two give no variance",
                                     "priority": "must"}
    text = (run.folder / "reviews" / "V1.md").read_text()
    assert "[must] Run five seeds" in text and "1. [pass] Data" in text
    brief = script.prompts("ReviewOutput")[0]
    assert "step_01: out.txt" in brief and "own work" in brief
    told = script.prompts("ExecDiscussorOutput")[1]
    assert "rating 5/10" in told and f"bootstrap --from {run.folder}" in told
    reviewer = [c for c in script.calls if c["schema"] == "ReviewOutput"][0]
    assert reviewer["access"].value == "read_only" and reviewer["thread"] is None


def test_the_user_writes_while_the_team_works_and_sets_a_breakpoint(run, script):
    ex = Execution(run)

    def owner(prompt):
        # The user writes while step 1 runs: answered on the side.
        out = ex.aside("What is step 1 doing? Stop after it, I want to look.")
        assert out.reply == "Counting rows; I will stop after step 1."
        return done_writing(run, "1")(prompt)

    script.add("ExecDecision", decide("run", "1"))
    script.add("StepReport", owner)
    script.add("Verdict", verdict())
    script.add("AsideOutput", {"reply": "Counting rows; I will stop after step 1.",
                               "breakpoints": [{"step": "1", "when": "after"}],
                               "requests": ["Log the row count per class"]})
    script.add("ExecDiscussorOutput", say("Step 1 passed: 1000 rows. Go on?"))
    turn = ex.start()
    assert turn.reply == "Step 1 passed: 1000 rows. Go on?" and ex.awaiting == "after:1"
    aside = script.prompts("AsideOutput")[0]
    assert "AN ASIDE" in aside and "Step 1 (builder): running, attempt 1" in aside
    told = script.prompts("ExecDiscussorOutput")[0]
    assert "PAUSED AFTER STEP 1" in told
    # The aside and the graph's turns are one conversation.
    disc = [c for c in script.calls if c["schema"] in ("AsideOutput", "ExecDiscussorOutput")]
    assert disc[1]["thread"] == disc[0]["thread"] or disc[0]["thread"] is None

    script.add("ExecDecision", decide("ask_user", task="Anything else?"))
    script.add("ExecDiscussorOutput", say("Going on."))
    ex.answer(go=True)
    orch = script.prompts("ExecDecision")[1]
    assert "- Log the row count per class" in orch, "the aside's request reaches the orchestrator"
    st = ex.research
    assert any(d["what"] == "Go on after step 1" for d in st["decisions"])
    assert not nodes.records(st)["1"]["pause_after"]


def test_a_breakpoint_before_a_step_holds_it(run, script):
    from research_agent.execute import inbox

    inbox.post(run.folder, "breakpoint", step="1", when="before", on=True)
    script.add("ExecDecision", decide("run", "1"))
    script.add("ExecDiscussorOutput", say("Step 1 is next; go?"))
    ex = Execution(run)
    ex.start()
    assert ex.awaiting == "1" and not script.prompts("StepReport")
    assert "WAITING FOR THE USER'S GO (a breakpoint)" in script.prompts("ExecDiscussorOutput")[0]
    assert inbox.peek(run.folder) == []


def test_a_stop_while_jobs_run_stops_them(run, script):
    ex = Execution(run)

    def launch(prompt):
        job = jobs.launch(run.folder, "1", "sleep 30")
        ex.stop_now()                                  # /stop, typed while it runs
        return {"state": "launched", "summary": "counting", "jobs": [job["id"]]}

    script.add("ExecDecision", decide("run", "1"))
    script.add("StepReport", launch)
    script.add("ExecDiscussorOutput", say("Stopped, as you asked."))
    turn = ex.start()
    assert turn.outcome == "stopped" and nodes.records(ex.research)["1"]["status"] == "stopped"
    assert all(jobs.status(j)["state"] != "running" for j in jobs.records(run.folder))
