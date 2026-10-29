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
    st, _ = S.apply(st, [{"op": "approve", "section": "design"}], by="user")
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


PAPER = {"summary": "The paper: 5/10.", "pdf": "", "problems": ["no LaTeX in the tests"]}
_A = {"score": 3, "why": "Table 1"}
#: One panel reviewer's form, and the meta-review that decides.
PANEL_REVIEW = {"summary": "A small, clean study.", "originality": _A, "significance": _A,
                "soundness": _A, "clarity": _A, "reproducibility": _A,
                "strengths": ["fixed seeds"], "weaknesses": ["two seeds only (Sec. 4)"],
                "questions": ["why these ranks?"], "missing_work": [], "ethics": "",
                "rating": 5, "justification": "too few seeds", "confidence": 4}
REVIEW = {"summary": "A small, clean study.", "consensus": ["two seeds only"],
          "disagreements": [], "decision": "weak reject", "rating": 5, "confidence": 4,
          "justification": "the effect is not shown to be stable", "requests": [
              {"what": "Run five seeds", "why": "two give no variance", "priority": "must"}]}


def reviewed(script, meta=REVIEW):
    """The panel's three reviews and the meta-review, scripted."""
    script.add("ReviewOutput", PANEL_REVIEW, PANEL_REVIEW, PANEL_REVIEW)
    script.add("MetaReviewOutput", meta)


def verdict(v="pass", **fields):
    return {"verdict": v, "summary": f"{v} it is", **fields}


def test_a_whole_execution_from_the_approved_design_to_done(run, script):
    def launch(prompt):
        assert "launch --step 2" in prompt, "the brief shows how to launch a job"
        job = jobs.launch(run.folder, "2", "echo trained > scores.txt", cwd=run.artifacts("2"))
        return {"state": "launched", "summary": "two seeds launched", "jobs": [job["id"]]}

    script.add("ExecDecision", decide("run", "1"), decide("run", "2"), decide("run", "2"),
               decide("done"))
    script.add("PaperReport", PAPER)
    reviewed(script)
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
    # Step 2 does not need step 1: the orchestrator may skip it alone.
    plan = {"steps": [PLAN["steps"][0], {**PLAN["steps"][1], "gate": False, "depends_on": []}]}
    folder = tmp_path / "run"
    S.Store(folder).write(approved_state(plan=plan))
    run = nodes.ExecRun(folder, {r: RoleConfig("fake") for r in (*config.ROLES, "discussor")},
                        skills={}, limits=replace(config.defaults(), poll_seconds=0.05),
                        quiet=True)
    script.add("ExecDecision", decide("skip", "1", "the protocol's T3 branch does not apply"),
               decide("run", "2"), decide("done"))
    script.add("PaperReport", PAPER)
    reviewed(script)
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
    with pytest.raises(ValueError, match="the design is not approved"):
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
    script.add("PaperReport", PAPER)
    reviewed(script)
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
    script.add("PaperReport", PAPER)
    reviewed(script)
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
    assert "step_01: out.txt" in brief and "own work" in brief and "YOUR FOCUS" in brief
    meta = script.prompts("MetaReviewOutput")[0]
    assert meta.count("REVIEWER R") == 3 and "weakness: two seeds only (Sec. 4)" in meta
    assert review["decision"] == "weak reject" and len(review["panel"]) == 3
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
    from research_agent import inbox

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


def test_steps_the_design_gives_its_orchestrator_are_run_by_an_owner(tmp_path, script):
    """Seen in a real design: the orchestrator node owned the preflight step.
    The orchestrator only decides; its step is done by an owner made from it."""
    design = {**DESIGN, "nodes": [{**DESIGN["nodes"][0], "name": "preflight_orchestrator",
                                   "steps": ["1"], "access": "write",
                                   "instructions": "Check the data first."},
                                  *DESIGN["nodes"][2:]]}
    folder = tmp_path / "run"
    S.Store(folder).write(approved_state(design=design))
    run = nodes.ExecRun(folder, {r: RoleConfig("fake") for r in (*config.ROLES, "discussor")},
                        skills={}, limits=replace(config.defaults(), poll_seconds=0.05,
                                                  confirm="none"), quiet=True)
    d = nodes.Design.read(S.Store(folder).read())
    assert d.owner_of == {"1": "preflight_orchestrator", "2": "trainer"}
    assert d.owners["preflight_orchestrator"]["role"] == "worker"
    assert d.orchestrator["name"] == "preflight_orchestrator"
    script.add("ExecDecision", decide("run", "1"), decide("ask_user", task="ok?"))
    script.add("StepReport", done_writing(run, "1"))
    script.add("Verdict", verdict())
    script.add("ExecDiscussorOutput", say("Step 1 passed."))
    Execution(run).start()
    brief = script.prompts("StepReport")[0]
    assert "You are preflight_orchestrator (worker).\nCheck the data first." in brief
    rec = nodes.records(S.Store(folder).read())["1"]
    assert rec["status"] == "pass" and rec["owner"] == "preflight_orchestrator"


def test_a_validator_that_owns_steps_is_refused():
    from research_agent.bootstrap.nodes import design_problems

    bad = {"nodes": [{**n, "steps": ["2"]} if n["role"] == "validator" else n
                     for n in DESIGN["nodes"]]}
    problems = design_problems(bad, steps=["1", "2"], rubric={"R1"}, skills=set())
    assert any(p.startswith("the validator checker owns steps 2, but it only checks")
               and "give those steps to a worker" in p for p in problems)


def test_the_user_can_give_a_failed_step_another_attempt_or_pass_it(run, script):
    """Seen in a real run: step 1 failed its three attempts on a rule the user
    had already waived, and nothing could move it on."""
    run.limits = replace(run.limits, max_attempts=1)
    script.add("ExecDecision", decide("run", "1"), decide("stop", task="step 1 failed its attempts"))
    script.add("StepReport", done_writing(run, "1"))
    script.add("Verdict", verdict("fail", problems=["the scorer has no license"], advice="get one"))
    script.add("ExecDiscussorOutput", say("Step 1 failed; stopped."))
    ex = Execution(run)
    ex.start()
    assert nodes.records(ex.research)["1"]["status"] == "fail"

    # "Use the unlicensed scorer as it is, and try step 1 again."
    script.add("ExecDiscussorOutput", say("Trying step 1 again.", retry=["1"],
                                          decisions=[{"what": "Use the scorer as it is",
                                                      "why": "research use"}]),
               say("Step 1 failed again."))
    script.add("ExecDecision", lambda prompt: decide("retry", "1") if "retry 1 (attempt 2 of 2)"
               in prompt else decide("ask_user", task="?"), decide("ask_user", task="Pass it?"))
    script.add("StepReport", done_writing(run, "1"))
    script.add("Verdict", verdict("fail", problems=["still unlicensed"], advice="x"))
    ex.answer("use the scorer as it is, and try step 1 again")
    rec = nodes.records(ex.research)["1"]
    assert rec["attempts"] == 2 and rec["extra_attempts"] == 1
    owner = script.prompts("StepReport")[-1]
    assert "THE USER'S DECISIONS" in owner and "Use the scorer as it is -- research use" in owner
    validator = script.prompts("Verdict")[-1]
    assert "THE USER'S DECISIONS" in validator and "Use the scorer as it is" in validator

    # "Pass it: the scorer is the official one; we use it as is."
    script.add("ExecDiscussorOutput", say("Step 1 passes on your decision.",
                                          waive=[{"step": "1", "why": "official scorer, used as is"}]),
               say("Step 2 next; go?"))
    script.add("ExecDecision", decide("run", "2"))
    ex.answer("pass step 1, the scorer is the official one")
    st = ex.research
    rec = nodes.records(st)["1"]
    assert rec["status"] == "pass" and rec["verdict"].startswith("passed by the user: official scorer")
    assert "the validator's verdict was: fail" in rec["verdict"] and \
        "still unlicensed" in rec["problems"], "what the validator said is kept beside it"
    assert ex.awaiting == "2", "the next step is open again (it is gated)"
    assert any(d["what"] == "Step 1 passes on the user's decision" for d in st["decisions"])


def test_the_rules_for_research_use_and_the_user_s_decisions():
    from research_agent.bootstrap import prompts as boot
    from research_agent.execute import prompts as exe

    for text in (exe.OWNER, exe.VALIDATOR, exe.ORCHESTRATOR):
        assert "never stop research use" in " ".join(text.split())
    assert set(boot.INSTRUCTIONS) == {"discussor", "orchestrator", "researcher", "designer"}


def test_autopilot_answers_a_stop_the_orchestrator_chose_but_not_the_users(run, script):
    from research_agent import autopilot

    autopilot.save(run.folder, autopilot.Settings(on=True, max_decisions=3))
    script.add("ExecDecision", *[decide("stop", task="step 1 used its attempts")] * 2)   # sent back once
    script.add("ExecDiscussorOutput", say("The team stopped: step 1 used its attempts."),
               say("Stopped, as you said."))
    script.add("AutopilotOutput", {"answer": "Stop here.", "why": "nothing can fix it",
                                   "stop": True})
    turn = Execution(run).start()
    asked = [c for c in script.calls if c["schema"] == "AutopilotOutput"]
    assert len(asked) == 1, "the orchestrator's stop is a question the autopilot answers"
    assert "The team stopped: step 1 used its attempts." in asked[0]["prompt"]
    assert turn.outcome == "stopped" and turn.reply == "Stopped, as you said."
    assert len([c for c in script.calls if c["schema"] == "AutopilotOutput"]) == 1, \
        "its own stop is final: it is not asked again"


def test_a_skipped_step_can_be_reopened_and_a_needed_one_is_not_skipped():
    # Run 20261027-2108: step 1 failed its last attempt, the orchestrator
    # skipped it and then step 2, and nothing could bring them back.
    st, _ = S.apply(approved_state(), [{"op": "set", "section": "execution", "value": {
        "steps": [{"id": "1", "title": "Data", "status": "fail", "attempts": 3},
                  {"id": "2", "title": "Train", "status": "pending"}]}}], by="execution")
    moves = nodes.open_moves(st, 3)
    assert moves["retry"] == [] and moves["skip"] == [], \
        "a failed step that step 2 needs is the user's call, and step 2 waits for it"
    st, _ = S.apply(st, [nodes.record_op(st, "1", status="skipped"),
                         ], by="execution")
    st, _ = S.apply(st, [nodes.record_op(st, "2", status="skipped")], by="execution")
    assert nodes.step_problems(st, ["1"], []) == []
    st, _ = S.apply(st, nodes.user_step_ops(st, ["1"], []), by="user")
    rec = nodes.records(st)
    assert rec["1"]["status"] == "stopped" and rec["2"]["status"] == "pending"
    assert nodes.open_moves(st, 3)["retry"] == ["1"], "one more attempt, as the user asked"
    assert nodes.step_problems(st, ["2"], []) == ["retry: step 2 has not failed or been "
                                                  "skipped (it is pending)"]


def test_the_autopilot_s_retry_gives_a_step_out_of_attempts_one_more(run, script):
    # Run 20261027-2108: the autopilot wrote "retry step 2" three times; in
    # words it gave no attempt, and the run stopped for good.
    from research_agent import autopilot

    run.limits = replace(run.limits, max_attempts=1)
    autopilot.save(run.folder, autopilot.Settings(on=True, max_decisions=1))
    script.add("ExecDecision", decide("run", "1"),
               decide("stop", task="step 1 used its attempts"),
               lambda prompt: decide("retry", "1", task="fix the rows") if "retry 1" in prompt
               else decide("ask_user", task="no retry offered"),
               decide("ask_user", task="step 2 next?"))
    script.add("StepReport", {"state": "failed", "summary": "rows missing"},
               done_writing(run, "1", "rows.json", "[1000]"))
    script.add("Verdict", verdict())
    script.add("ExecDiscussorOutput", say("Step 1 failed and has no attempts left."),
               say("One more attempt at step 1, as you said."),
               say("Step 1 passed. Step 2 next?"))
    script.add("AutopilotOutput", {"answer": "Try step 1 once more.", "why": "fixable",
                                   "retry": ["1"]})
    turn = Execution(run).start()
    asked = script.prompts("AutopilotOutput")[0]
    assert "OUT OF ATTEMPTS" in asked and "step 1 fail: 1 of 1 attempts used" in asked
    st = Execution(run).research
    rec = nodes.records(st)["1"]
    assert rec["status"] == "pass" and rec["extra_attempts"] == 1
    assert any(d["what"] == "Another attempt at step 1" and d["by"] == "autopilot"
               for d in st["decisions"])
    assert turn.reply == "Step 1 passed. Step 2 next?"
    assert nodes.step_problems(st, ["1"], [], 1) and \
        nodes.step_problems({**st}, ["1"], []) == ["retry: step 1 has not failed or been "
                                                   "skipped (it is pass)"]


def test_asking_the_user_about_an_untried_step_goes_back_once(run, script):
    # Run 20261028-1657: the orchestrator asked the user for "a writable
    # environment" instead of running the preflight step, whose owner has it.
    st = Execution(run).research
    st, _ = S.apply(st, [{"op": "set", "section": "execution", "value": {
        "steps": [{"id": "1", "title": "Data", "status": "pending"},
                  {"id": "2", "title": "Train", "status": "pending"}]}}], by="execution")
    design = nodes.Design.read(st)
    script.add("ExecDecision", decide("ask_user", "1", task="Provide a writable environment"),
               decide("run", "1"))
    out = nodes.orchestrator(run, st, design, budget="", requests=[])
    assert out.data.action == "run" and out.data.step == "1"
    assert "has not been tried and is open to run" in script.prompts("ExecDecision")[1]
    script.add("ExecDecision", decide("ask_user", "1", task="Which tier?"),
               decide("ask_user", "1", task="Which tier, really?"))
    out = nodes.orchestrator(run, st, design, budget="", requests=[])
    assert out.data.action == "ask_user", "a question it insists on still reaches the user"
    st, _ = S.apply(st, [nodes.record_op(st, "1", status="fail", attempts=1,
                                         problems=["nvidia-smi cannot reach the driver"])],
                    by="execution")
    script.add("ExecDecision", decide("ask_user", "1", task="Provide GPUs"),
               decide("retry", "1", task="use the GPUs; the last attempt was sandboxed"))
    out = nodes.orchestrator(run, st, design, budget="", requests=[])
    assert out.data.action == "retry"
    assert "step 1 can be retried" in script.prompts("ExecDecision")[-1]


def test_an_owner_s_failure_with_outputs_is_the_validator_s_to_judge(run, script):
    # Run 20261028-1657: the preflight owner did its step and then failed it
    # on a criterion steps 2 and 3 deliver; the validator never saw it.
    def failed_with_report(prompt):
        folder = run.artifacts("1")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "preflight.md").write_text("checkpoint loads; probes pass")
        return {"state": "failed", "summary": "probes pass; R5 not yet audited",
                "artifacts": ["preflight.md"], "problems": ["dedup not done"]}

    script.add("ExecDecision", decide("run", "1"), decide("ask_user", task="step 2 next?"))
    script.add("StepReport", failed_with_report)
    script.add("Verdict", verdict("pass", summary="its own check holds; R5 is steps 2-3's"))
    script.add("ExecDiscussorOutput", say("Step 1 passed. Step 2 next?"))
    Execution(run).start()
    rec = nodes.records(Execution(run).research)["1"]
    assert rec["status"] == "pass" and len(script.prompts("Verdict")) == 1


def test_the_autopilot_can_pass_a_step_that_did_its_part(run, script):
    from research_agent import autopilot

    run.limits = replace(run.limits, max_attempts=1)
    autopilot.save(run.folder, autopilot.Settings(on=True, max_decisions=1))
    script.add("ExecDecision", decide("run", "1"),
               decide("stop", task="step 1 used its attempts"),
               decide("ask_user", task="step 2 next?"))
    script.add("StepReport", {"state": "failed", "summary": "probes pass; R5 is later steps'"})
    script.add("ExecDiscussorOutput", say("Step 1 failed and has no attempts left."),
               say("Step 1 passes, as you decided."), say("Step 2 next; go?"),
               say("Waiting."))
    script.add("AutopilotOutput", {"answer": "Pass step 1.", "why": "its own check holds",
                                   "waive": ["1"]})
    Execution(run).start()
    st = Execution(run).research
    rec = nodes.records(st)["1"]
    assert rec["status"] == "pass" and "passed by the user: autopilot: its own check" in rec["verdict"]
    assert any(d["what"] == "Step 1 passes on the autopilot's decision" for d in st["decisions"])


def test_rewind_starts_over_from_a_step_and_keeps_the_outputs(run, monkeypatch):
    st, _ = S.apply(approved_state(), [{"op": "set", "section": "execution", "value": {
        "steps": [{"id": "1", "title": "Data", "status": "pass", "attempts": 2,
                   "verdict": "pass", "artifacts": ["rows.json"]},
                  {"id": "2", "title": "Train", "status": "fail", "attempts": 3,
                   "extra_attempts": 1}]}}], by="execution")
    for sid in ("1", "2"):
        run.artifacts(sid).mkdir(parents=True, exist_ok=True)
        (run.artifacts(sid) / "out.txt").write_text(sid)
    assert nodes.rewind_problems(run.folder, st, "1") == []
    st, _ = S.apply(st, nodes.rewind(run.folder, st, "1"), by="user")
    rec = nodes.records(st)
    assert [(r["status"], r["attempts"], r["extra_attempts"], r["verdict"]) for r in rec.values()] \
        == [("pending", 0, 0, "")] * 2, "step 1 and the step that needs it start over"
    kept = list((run.folder / "artifacts" / nodes.REWOUND).iterdir())
    assert len(kept) == 1 and kept[0].name.endswith("-to-step-1")
    assert sorted(p.name for p in kept[0].iterdir()) == ["step_01", "step_02"]
    assert not run.artifacts("1").exists()
    assert st["decisions"][-1]["what"] == "Rewind to step 1: steps 1, 2 start over"
    assert nodes.rewind_problems(run.folder, st, "1") == ["rewind: step 1 has not run yet"]
    st, _ = S.apply(st, [nodes.record_op(st, "1", status="pass"),
                         ], by="x")
    st, _ = S.apply(st, [nodes.record_op(st, "2", status="waiting")], by="x")
    assert "still running: step 2 (waiting)" in nodes.rewind_problems(run.folder, st, "1")[0]
    assert nodes.rewind_problems(run.folder, st, "9") == ["rewind: there is no step 9"]


def test_the_user_can_ask_the_discussor_to_rewind(run, script):
    script.add("ExecDecision", decide("run", "1"), decide("ask_user", task="Step 2 next?"),
               decide("ask_user", task="Start over: run 1?"))
    script.add("StepReport", done_writing(run, "1", "rows.json", "[1000]"))
    script.add("Verdict", verdict())
    script.add("ExecDiscussorOutput", say("Step 1 passed. Step 2 next?"),
               say("Going back to step 1.", hand_to="orchestrator", rewind="1"),
               say("Step 1 starts over. Run it?"))
    session = Execution(run)
    session.start()
    turn = session.answer("Rewind to step 1, the rows were wrong.")
    assert turn.reply == "Step 1 starts over. Run it?"
    rec = nodes.records(session.research)["1"]
    assert rec["status"] == "pending" and rec["attempts"] == 0
    assert any(p.name.endswith("-to-step-1")
               for p in (run.folder / "artifacts" / nodes.REWOUND).iterdir())


def test_the_paper_is_written_and_its_pdf_checked(run, script):
    run.limits = replace(run.limits, latex="/usr/bin/tectonic")
    st = approved_state()
    script.add("PaperReport", {**PAPER, "pdf": "main.pdf", "problems": []},
               {**PAPER, "pdf": "", "problems": []}, {**PAPER, "pdf": "", "problems": ["no fonts"]})
    out = nodes.paper(run, st)
    retries = script.prompts("PaperReport")
    assert "there is no PDF at main.pdf" in retries[1] and "`pdf` is empty" in retries[2]
    assert f"cd {run.folder / 'paper'} && /usr/bin/tectonic main.tex" in retries[0]
    assert out.ops[0]["value"] == {"paper": ""} and "no fonts" in out.ops[1]["value"]["summary"]

    (run.folder / "paper" / "main.pdf").write_bytes(b"%PDF-1.4")
    script.add("PaperReport", {**PAPER, "pdf": "main.pdf", "problems": []})
    out = nodes.paper(run, st)
    assert out.ops[0]["value"] == {"paper": str(run.folder / "paper" / "main.pdf")}
    assert "PAPER -- the submission" in nodes.views.reviewer(st, run_dir=str(run.folder))


def test_the_design_stage_s_requests_do_not_stop_the_execution(run, script):
    from research_agent import inbox
    S.Store(run.folder).write(approved_state())
    inbox.post(run.folder, "request", text="Execution remains user-started; do not run anything.")
    script.add("ExecDecision", decide("stop", task="the discussor said not to run"),
               decide("stop", task="still"))
    script.add("ExecDiscussorOutput", say("Stopped."))
    Execution(run).start()
    first, retry = script.prompts("ExecDecision")
    assert "do not run anything" not in first, "a request from the design stage is dropped"
    assert "`stop` ends the execution, and step 1 is open to run and untried" in retry


def test_a_step_short_of_what_a_skipped_step_left_out_passes_as_partial(run, script):
    # Run 1455-next: a runtime gate cancelled the expanded fits (step 4 was
    # skipped), and the analysis failed for want of them, stopping the run.
    st, _ = S.apply(approved_state(), [{"op": "set", "section": "execution", "value": {
        "steps": [{"id": "1", "title": "Data", "status": "skipped"},
                  {"id": "2", "title": "Train", "status": "validating", "attempts": 1}]}}],
        by="execution")
    design = nodes.Design.read(st)
    node, step = design.owners[design.owner_of["2"]], nodes.plan_step(st, "2")
    script.add("Verdict", verdict("partial"), verdict("partial", problems=["no expanded fits"]))
    out = nodes.validator(run, st, design, node=node, step=step, report={})
    assert "say in `problems` what is missing" in script.prompts("Verdict")[1]
    rec = nodes.records(S.apply(st, out.ops, by="v")[0])["2"]
    assert rec["status"] == "pass" and rec["verdict"].startswith("partial (what steps 1 did not")
    assert nodes.short(rec) and "no expanded fits" in rec["problems"]

    st, _ = S.apply(st, [nodes.record_op(st, "1", status="pass", verdict="pass: ok")], by="x")
    script.add("Verdict", verdict("partial", problems=["x"]), verdict())
    nodes.validator(run, st, design, node=node, step=step, report={})
    assert "none of the steps it needs is" in script.prompts("Verdict")[-1]


def test_the_autopilot_does_not_stop_over_a_step_it_could_still_pass(run, script):
    from research_agent import autopilot
    st, _ = S.apply(approved_state(), [{"op": "set", "section": "execution", "value": {
        "steps": [{"id": "1", "title": "Data", "status": "fail", "attempts": 1},
                  {"id": "2", "title": "Train", "status": "pending"}]}}], by="execution")
    stop = {"answer": "Stop.", "why": "w", "stop": True}
    script.add("AutopilotOutput", stop, stop)
    out = autopilot.decide(run, st, stage="execution", reply="Step 1 failed.")
    assert out.stop and len(script.prompts("AutopilotOutput")) == 2, "a stop again is final"
    assert "`waive` a step that did its own part" in script.prompts("AutopilotOutput")[1]


def test_only_the_user_skips_a_step_later_steps_need(run, script):
    # Run 1455-next: a runtime gate failed by 57 s, step 4 was skipped and
    # step 5, the main analysis, could no longer be done.
    from research_agent import autopilot
    autopilot.save(run.folder, autopilot.Settings(on=True, max_decisions=5))
    script.add("ExecDecision", decide("skip", "1", "the gate failed"),
               decide("ask_user", "1", "Step 1 is over the limit: raise it, or skip it?"))
    script.add("ExecDiscussorOutput", say("Step 1 is over the limit. Raise it, or skip it?"))
    ex = Execution(run)
    turn = ex.start()
    first, retry = script.prompts("ExecDecision")
    assert "skip 1: steps 2 need it" in retry and "Only the user decides that" in retry
    assert not [c for c in script.calls if c["schema"] == "AutopilotOutput"], \
        "giving up a step later steps need is handed back, not answered by the autopilot"
    assert "handed back" in autopilot.load(run.folder).handed_back
    autopilot.save(run.folder, autopilot.Settings(on=False))

    script.add("ExecDiscussorOutput", say("Skipping step 1, as you say.",
                                          skip=[{"step": "1", "why": "not worth the time"}]))
    script.add("ExecDecision", decide("run", "2"))
    script.add("ExecDiscussorOutput", say("Step 2 waits for your go."))
    ex.answer("Skip step 1.")
    st = ex.research
    rec = nodes.records(st)["1"]
    assert rec["status"] == "skipped" and rec["summary"] == "skipped by the user: not worth the time"
    assert st["decisions"][-1]["what"] == "Skip step 1" and st["decisions"][-1]["by"] == "user"


def test_the_autopilot_s_answer_is_not_recorded_as_the_user_s(run, script):
    from research_agent import autopilot
    said = say("Going on.", decisions=[{"what": "Keep going", "why": "it said so"}])
    script.add("ExecDiscussorOutput", said, said)
    turn = nodes.discussor(run, approved_state(), seen={}, user=f"{autopilot.MARK} Keep going.")
    assert not [o for o in turn.ops if o.get("section") == "decisions"], \
        "the autopilot recorded its own answer; the discussor's copy is not the user's"
    turn = nodes.discussor(run, approved_state(), seen={}, user="Keep going.")
    assert [o["value"]["by"] for o in turn.ops if o.get("section") == "decisions"] == ["user"]
