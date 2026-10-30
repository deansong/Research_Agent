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
from research_agent.execute import config, jobs, nodes, views


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
    assert "You are trainer. Train it.\nThe work: two seeds" in brief
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
    assert "follows these parts of the approved protocol: Data, Metrics" in brief
    for gone in ("GoEmotions at /data", "macro-F1", "paired bootstrap", "42, 43, 44", "Qwen3-4B",
                 "Use GoEmotions"):
        assert gone not in brief
    assert "Drop the neutral label" in brief, "a decision since the execution started"
    assert str(tmp_path / "state" / "research.md") in brief, "the rest, where to find it"


def test_before_the_execution_starts_no_decision_is_repeated_to_an_owner():
    # Run 1119, back in the design stage: the preview showed every decision.
    st, _ = S.apply(S.new(), [{"op": "add", "section": "decisions", "value": {
        "what": "Approve design v3", "by": "user", "at": "2026-10-30T14:12:04+00:00"}}], by="x")
    assert views.user_decisions(st, since_execution=True) == ""
    assert "Approve design v3" in views.user_decisions(st)
