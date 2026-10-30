"""
WHAT:  The execution stage's parts that need no model: its configuration, the
       jobs helper, and the briefs.

Run with:   python -m pytest tests/test_execute_parts.py -q
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from research_agent import state as S
from research_agent.bootstrap.config import RoleConfig
from research_agent.execute import config, jobs, nodes, views
from tests.scripted import script  # noqa: F401 -- fixture


def test_the_defaults_and_a_run_s_file_then_the_command_line(tmp_path):
    import tomllib

    cfg = config.defaults()
    toml = tomllib.loads(config.DEFAULT_FILE.read_text())
    worker = toml["roles"]["worker"]
    assert cfg.roles["worker"].provider == worker["provider"] and cfg.confirm == "gated"
    assert cfg.roles["worker"].options == worker.get("options", {})
    assert set(cfg.roles) == set(config.ROLES) and config.check(cfg) == []
    (tmp_path / "execute.json").write_text(json.dumps({
        "roles": {"validator": {"provider": "claude_code", "model": "sonnet"}},
        "execute": {"gpu_hours": 140, "confirm": "all"}}))
    cfg = config.load(tmp_path, roles=["worker=codex::high"], limits={"wall_hours": 24,
                                                                        "confirm": None})
    assert (cfg.gpu_hours, cfg.wall_hours, cfg.confirm) == (140, 24, "all")
    assert str(cfg.roles["validator"]) == "claude_code:sonnet"
    assert str(cfg.roles["worker"]) == "codex:high"
    config.save(tmp_path, cfg)
    assert config.load(tmp_path) == cfg
    with pytest.raises(ValueError, match="confirm is 'often'"):
        config.load(None, limits={"confirm": "often"})
    with pytest.raises(ValueError, match="No role 'chef'"):
        config.load(None, roles=["chef=codex"])


def _wait(run, job_id, seconds=10):
    end = time.time() + seconds
    while jobs.status(jobs.get(run, job_id))["state"] == "running" and time.time() < end:
        time.sleep(0.05)
    return jobs.status(jobs.get(run, job_id))


def test_a_job_runs_detached_and_leaves_its_record(tmp_path):
    job = jobs.launch(tmp_path, "5", ["sh", "-c", "echo hello; exit 3"], name="seed17",
                      cwd=tmp_path)
    assert job["id"] == "s5-001" and job["gpus"] == []
    done = _wait(tmp_path, job["id"])
    assert done["state"] == "finished" and done["exit"] == 3
    assert jobs.tail(jobs.get(tmp_path, "s5-001")) == "hello"
    assert jobs.launch(tmp_path, "5", "true")["id"] == "s5-002"
    assert [j["id"] for j in jobs.records(tmp_path, "5")] == ["s5-001", "s5-002"]
    assert jobs.records(tmp_path, "6") == []


def test_a_job_can_be_killed_and_gpus_are_counted(tmp_path, monkeypatch):
    job = jobs.launch(tmp_path, "2", "sleep 30")
    assert jobs.status(job)["state"] == "running"
    assert jobs.kill(tmp_path, job["id"]) and _wait(tmp_path, job["id"])["state"] != "running"
    # GPU-hours: the hours a job ran times its GPUs.
    record = {**job, "gpus": [0, 1], "started": time.time() - 3600}
    (tmp_path / "jobs" / f"{job['id']}.json").write_text(json.dumps(record))
    assert jobs.gpu_hours(tmp_path, "2") == pytest.approx(2.0, abs=0.05)


def test_gpus_are_picked_free_and_not_twice(tmp_path, monkeypatch):
    smi = "0, 0\n1, 30000\n2, 10\n"
    monkeypatch.setattr(jobs.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout=smi))
    assert jobs.pick_gpus(tmp_path, 2) == [0, 2]
    running = {"id": "s1-001", "step": "1", "gpus": [0], "pid": 1, "log": str(tmp_path / "x"),
               "started": time.time(), "command": "x"}
    (tmp_path / "jobs" / "s1-001.json").write_text(json.dumps(running))
    monkeypatch.setattr(jobs, "_alive", lambda pid: True)
    assert jobs.pick_gpus(tmp_path, 1) == [2]
    with pytest.raises(jobs.JobError, match="2 GPUs asked for, 1 free"):
        jobs.pick_gpus(tmp_path, 2)


def test_the_helper_runs_as_a_plain_script(tmp_path):
    out = subprocess.run([sys.executable, jobs.__file__, "--run", str(tmp_path), "launch",
                          "--step", "3", "--", "echo", "hi"], capture_output=True, text=True,
                         cwd=tmp_path)
    assert out.returncode == 0 and out.stdout.strip() == "s3-001"
    _wait(tmp_path, "s3-001")
    listed = subprocess.run([sys.executable, jobs.__file__, "--run", str(tmp_path), "status"],
                            capture_output=True, text=True).stdout
    assert "s3-001 [finished, exit 0]" in listed


def test_an_owner_s_brief_has_its_step_its_rubric_and_where_to_work(tmp_path):
    st = S.new()
    st, _ = S.apply(st, [
        {"op": "add", "section": "rubric", "value": {"criterion": "Seeds fixed"}},
        {"op": "add", "section": "rubric", "value": {"criterion": "Unrelated"}},
        {"op": "set", "section": "plan", "value": {"steps": [
            {"id": "1", "title": "Data", "check": "rows counted"},
            {"id": "2", "title": "Train", "detail": "two seeds", "depends_on": ["1"],
             "rubric": ["R1"]}]}},
        {"op": "set", "section": "execution", "value": {"steps": [
            {"id": "1", "title": "Data", "status": "pass", "summary": "1000 rows",
             "artifacts": ["rows.json"]},
            {"id": "2", "title": "Train"}]}}], by="x")
    node = {"name": "trainer", "role": "worker", "instructions": "Train it.", "validated_by": []}
    paths = {"run": str(tmp_path), "work": str(tmp_path / "work"),
             "artifacts": str(tmp_path / "artifacts" / "step_02")}
    brief = views.owner(st, node=node, step=st["plan"]["steps"][1], task="", paths=paths,
                        jobs_command="python jobs.py --run R")
    # A work order: what to deliver and how it is checked, in plain words --
    # no step numbers, rubric ids, cost, goal or hypotheses.
    assert "You are trainer.\nThe work: two seeds" in brief and "Train it." not in brief
    assert "- Seeds fixed" in brief and "Unrelated" not in brief and "R1" not in brief
    assert "Data: " in brief and "rows.json" in brief and "step_01" in brief
    for gone in ("Step 2:", "After steps", "Rubric:", "Cost:", "THE STUDY", "HYPOTHESES"):
        assert gone not in brief
    assert "python jobs.py --run R launch --step 2" in brief
    assert "The study's Python" not in brief
    brief = views.owner(st, node=node, step=st["plan"]["steps"][1], task="",
                        paths={**paths, "python": "/envs/train/bin/python"},
                        jobs_command="python jobs.py --run R")
    assert "The study's Python (torch, CUDA, its libraries)" in brief
    assert "-- /envs/train/bin/python train.py --seed 17" in brief
    assert nodes.access_for({**node, "access": "read_only"}) == "full", \
        "a step owner always runs with full access"


def test_an_older_run_s_runner_is_a_worker(tmp_path):
    (tmp_path / "execute.json").write_text(json.dumps({"roles": {"runner": {"provider": "x"}}}))
    cfg = config.load(tmp_path)
    assert "runner" not in cfg.roles and "worker" in cfg.roles
    assert nodes.role_of({"role": "runner"}) == "worker"


def test_an_owner_gets_only_the_rules_its_step_follows_and_decisions_since_the_start(tmp_path):
    # Run 1119: a data-preparation step got the whole protocol (statistics,
    # seeds, stop criteria), the models and budget, and the user's twelve
    # decisions from before the design was approved.
    st, _ = S.apply(S.new(), [
        {"op": "set", "section": "protocol", "value": {
            "metrics": ["macro-F1 over held-out labels"], "seeds": "42, 43, 44",
            "statistics": "paired bootstrap"}},
        {"op": "merge", "section": "constraints", "value": {
            "data": "GoEmotions at /data/emotions", "models": "Qwen3-4B"}},
        {"op": "add", "section": "decisions", "value": {
            "what": "Use GoEmotions", "by": "user", "at": "2026-10-30T10:00:00+00:00"}},
        {"op": "set", "section": "plan", "value": {"steps": [
            {"id": "1", "title": "Prepare the data", "detail": "splits", "check": "files",
             "follows": ["data", "metrics"]}]}},
        {"op": "set", "section": "execution", "value": {
            "started": "2026-10-30T12:00:00+00:00", "steps": [{"id": "1", "title": "Prepare"}]}},
        {"op": "add", "section": "decisions", "value": {
            "what": "Drop the neutral label", "by": "user", "at": "2026-10-30T13:00:00+00:00"}}],
        by="x")
    node = {"name": "data_preparer", "role": "worker", "instructions": "Prepare."}
    paths = {"run": str(tmp_path), "work": str(tmp_path / "w"), "artifacts": str(tmp_path / "a")}
    brief = views.owner(st, node=node, step=st["plan"]["steps"][0], task="", paths=paths,
                        jobs_command="jobs")
    assert "research.md" not in brief, "no protocol, not even pointed to"
    for gone in ("GoEmotions at /data", "macro-F1", "paired bootstrap", "42, 43, 44", "Qwen3-4B",
                 "Use GoEmotions"):
        assert gone not in brief
    assert "Drop the neutral label" in brief, "a decision since the execution started"


def test_before_the_execution_starts_no_decision_is_repeated_to_an_owner():
    # Run 1119, back in the design stage: the preview showed every decision.
    st, _ = S.apply(S.new(), [{"op": "add", "section": "decisions", "value": {
        "what": "Approve design v3", "by": "user", "at": "2026-10-30T14:12:04+00:00"}}], by="x")
    assert views.user_decisions(st, since_execution=True) == ""
    assert "Approve design v3" in views.user_decisions(st)


def test_a_step_starts_only_with_its_inputs_checked_and_its_context_written(tmp_path, script):
    # Run 1119: an owner told to "execute B-E" was never told what B-E are;
    # design v1's steps read files from folders that did not exist.
    st, _ = S.apply(S.new(), [
        {"op": "merge", "section": "protocol", "value": {"seeds": "17, 29, 43"}},
        {"op": "set", "section": "plan", "value": {"steps": [
            {"id": "1", "title": "Data", "detail": "split it", "check": "splits written"},
            {"id": "2", "title": "Train", "detail": "train it", "check": "c",
             "depends_on": ["1"], "follows": ["seeds"]}]}},
        {"op": "set", "section": "execution", "value": {"steps": [
            {"id": "1", "title": "Data", "status": "pass", "summary": "train/dev written"},
            {"id": "2", "title": "Train", "status": "pending"}]}}], by="x")
    run = nodes.ExecRun(tmp_path, {r: RoleConfig("fake") for r in (*config.ROLES, "discussor")},
                        skills={}, limits=config.defaults(), quiet=True)
    (tmp_path / "artifacts" / "step_01").mkdir(parents=True)
    (tmp_path / "artifacts" / "step_01" / "train.jsonl").write_text("{}\n")
    bad = {"ready": True, "inputs": [{"path": "artifacts/03_data/train.jsonl", "holds": "x"}],
           "context": "go", "missing": [{"what": "dev split", "from_step": "1"},
                                        {"what": "the Qwen tokenizer", "from_step": ""},
                                        {"what": "execution/delivery_receipts/a.json",
                                         "from_step": "1"}]}
    good = {"ready": True, "context": "Train with seeds 17, 29 and 43, as the protocol fixes.",
            "inputs": [{"path": "artifacts/step_01/train.jsonl", "holds": "the training split"}],
            "obtain": ["Qwen/Qwen3-4B-Instruct-2507's tokenizer, from Hugging Face"]}
    script.add("DispatchOutput", bad, good)
    node = {"name": "trainer", "role": "worker"}
    out = nodes.dispatch(run, st, node=node, step=nodes.plan_step(st, "2"), note="first run")
    first, retry = script.prompts("DispatchOutput")
    assert "artifacts/step_01/train.jsonl" in first, "the files there now, listed by code"
    assert "Seeds: 17, 29, 43" in first and "THE ORCHESTRATOR'S NOTE ===\nfirst run" in first
    for expected in ("input artifacts/03_data/train.jsonl does not exist",
                     "ready, but `missing` lists", "the step follows seeds",
                     "missing 'the Qwen tokenizer': (no step) is not an earlier step",
                     "put it in `obtain`", "execution/ is kept by code"):
        assert expected in retry
    assert out.data.ready and out.data.inputs[0].path == "artifacts/step_01/train.jsonl"

    brief = views.owner(st, node=node, step=nodes.plan_step(st, "2"), task="use bf16",
                        paths=run.paths("2"), jobs_command="jobs",
                        dispatch={"inputs": good["inputs"], "context": good["context"],
                                  "obtain": good["obtain"]})
    assert "TO OBTAIN" in brief and "- Qwen/Qwen3-4B-Instruct-2507's tokenizer" in brief
    assert "checked to exist before you started ===\n- artifacts/step_01/train.jsonl: the " \
           "training split" in brief
    assert "seeds 17, 29 and 43" in brief and "From the orchestrator: use bf16" in brief


def test_code_keeps_the_execution_start_and_tells_the_dispatch_the_owner_can_write(tmp_path,
                                                                                  script):
    # Run 1119: a planned step required execution/execution_start.json, which
    # nothing wrote, and the dispatch -- read-only itself -- said the worker
    # could not write.
    st, _ = S.apply(S.new(), [
        {"op": "add", "section": "decisions", "value": {"what": "Start execution.", "by": "user"}},
        {"op": "set", "section": "plan", "value": {"steps": [
            {"id": "1", "title": "Data", "detail": "split it", "check": "splits written"}]}},
        {"op": "set", "section": "execution", "value": {
            "started": "2999-01-01T00:00:00+00:00",
            "steps": [{"id": "1", "title": "Data", "status": "pending"}]}}], by="x")
    run = nodes.ExecRun(tmp_path, {r: RoleConfig("fake") for r in (*config.ROLES, "discussor")},
                        skills={}, limits=config.defaults(), quiet=True)
    path = nodes.start_record(run, st)
    record = json.loads(path.read_text())
    assert path == tmp_path / "execution" / "execution_start.json"
    assert record["started_at_utc"] == "2999-01-01T00:00:00+00:00"
    assert record["execution_id"] == tmp_path.name
    assert record["initiated_by"]["what"] == "Start execution."
    path.write_text("{}")
    assert nodes.start_record(run, st) == path and path.read_text() == "{}", "written once"

    nodes.dispatch(run, st, node={"name": "prep", "role": "worker"},
                   step=nodes.plan_step(st, "1"))
    brief = script.prompts("DispatchOutput")[0]
    assert "Its owner runs with full access" in brief and "execution_start.json" in brief
    assert "downloads datasets, models and packages itself" in brief
    owner = views.owner(st, node={"name": "prep", "role": "worker"},
                        step=nodes.plan_step(st, "1"), task="", paths=run.paths("1"),
                        jobs_command="jobs")
    assert "The execution started at 2999-01-01T00:00:00+00:00" in owner


def test_an_earlier_execution_s_jobs_and_outputs_are_set_aside(tmp_path):
    # Run 1119: after Back to design, step 1's validator counted the earlier
    # execution's jobs s1-001..003 as the step's own, and failed it for an
    # archived copy of the test labels readable by others.
    run = nodes.ExecRun(tmp_path, {r: RoleConfig("fake") for r in (*config.ROLES, "discussor")},
                        skills={}, limits=config.defaults(), quiet=True)
    folder = jobs.folder(tmp_path)
    for job_id, at in (("s1-001", "2026-10-30T13:24:39+00:00"),
                       ("s1-004", "2026-10-30T17:54:44+00:00")):
        log = folder / f"{job_id}.log"
        log.write_text("done\n")
        (folder / f"{job_id}.exit").write_text("0")
        (folder / f"{job_id}.json").write_text(json.dumps({
            "id": job_id, "step": "1", "pid": 0, "gpus": [0], "log": str(log),
            "started": 0, "started_at": at}))
    kept = tmp_path / "artifacts" / "_before_redesign-20261030-135107"
    kept.mkdir(parents=True)
    kept.chmod(0o775)
    st = {"execution": {"started": "2026-10-30T17:11:03+00:00", "steps": []}}
    nodes.set_aside_earlier(run, st)
    assert [j["id"] for j in jobs.records(tmp_path, "1")] == ["s1-004"], "this execution's only"
    assert (folder / "_earlier" / "s1-001.json").exists() and (folder / "_earlier" / "s1-001.log").exists()
    assert kept.stat().st_mode & 0o777 == 0o700, "only the run's owner reads what is kept"


def test_the_discussor_sees_how_far_a_running_job_has_got(tmp_path):
    # Run 1119: asked when step 2's job would finish, the discussor had only
    # "Working now: step 2", and asked the team to report its progress.
    out = tmp_path / "artifacts" / "step_02"
    out.mkdir(parents=True)
    (out / "prompts.jsonl").write_text("{}\n" * 2000)
    (out / "responses.jsonl").write_text("{}\n" * 920)
    job = jobs.launch(tmp_path, "2", "sleep 30", name="teacher_generation")
    try:
        seen = views.running_jobs(tmp_path)
        assert f"{job['id']} (teacher_generation), step 2: running" in seen
        assert "prompts.jsonl: 2000 lines" in seen and "responses.jsonl: 920 lines" in seen
        assert "being written" not in seen.split("prompts.jsonl")[1].split("\n")[0], \
            "files written before the job started are not its output"
        brief = views.aside({"execution": {"steps": []}}, user="when will it finish?",
                            activity="Working now: step 2", run_dir=str(tmp_path))
        assert "RUNNING JOBS -- measured by code just now" in brief
        assert "estimate it yourself" in brief
    finally:
        jobs.kill(tmp_path, job["id"])


def test_what_only_the_system_can_give_is_never_a_step_s_to_deliver():
    # Run 1119: D12 ("report results after each experiment before the next")
    # became a delivery receipt in 11 steps' checks -- which nothing makes --
    # and every one of those steps failed on it, while none had a gate.
    from research_agent import prompt_files

    def said(name):
        return " ".join(prompt_files.text(name).split())

    assert "set `gate` on the next step" in said("notes/step_reader")
    assert "never a message, a delivery, a receipt" in said("notes/step_reader")
    assert "the user wants to see the results before it" in said("roles/bootstrap/planner")
    assert "a receipt, approval, delivery or guarantee" in said("roles/bootstrap/reader")
    assert "never fail a step for it" in said("roles/execute/validator")
    assert "it is not a failure" in said("roles/execute/owner")
    assert "Never missing, either: what only the system gives" in said("roles/execute/dispatch")
