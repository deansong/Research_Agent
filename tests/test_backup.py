"""Backups: a role whose model is out of credits (or unreachable) moves to its
backups, in order, until the model's limit resets."""

from __future__ import annotations

import json
import time

import pydantic
import pytest

import research_backend as RB
from research_agent.bootstrap import config
from research_agent.bootstrap.config import RoleConfig
from research_agent.run import Run
from research_backend.base import Access, StructuredRun, Usage


class Out(pydantic.BaseModel):
    answer: str


class Model:
    """One provider: answers with its name, or fails as told."""

    max_access = Access.FULL
    on_progress = None

    def __init__(self, name, fails=None):
        self.name, self.fails, self.calls = name, fails, []

    def run_structured(self, *, thread_id, prompt, output_model, **_):
        self.calls.append({"thread": thread_id, "prompt": prompt})
        if self.fails:
            raise self.fails
        return StructuredRun(data=output_model.model_validate({"answer": self.name}),
                             thread_id=thread_id or f"{self.name}-thread-{len(self.calls)}",
                             is_new_thread=thread_id is None, usage=Usage(), events=[])


@pytest.fixture()
def models(monkeypatch):
    made = {"codex": Model("codex"), "claude_code": Model("claude_code"),
            "antigravity": Model("antigravity")}
    options = []

    def backend(provider, model, opts, **_):
        options.append((provider, dict(opts)))
        return made[provider]
    monkeypatch.setattr("research_backend.conversation._backend", backend)
    made["options"] = options
    return made


def _run(tmp_path, cfg):
    return Run(tmp_path, roles={"orchestrator": cfg, "discussor": cfg}, quiet=True)


def test_out_of_credits_moves_to_the_backup_and_stays(tmp_path, models):
    models["codex"].fails = RB.BackendOutOfCredits("usage limit reached, try again in 2 hours")
    run = _run(tmp_path, RoleConfig("codex", backup=("claude_code:sonnet",)))
    answer, _ = run.call("orchestrator", brief="go", schema=Out)
    assert answer.answer == "claude_code"
    # The model waits for nobody: its backup is there.
    assert ("codex", {"credit_wait_hours": 0, "busy_attempts": 1}) in models["options"]
    # Benched until the time it named (two hours), not the default hour.
    assert 7100 < run.benched["codex"] - time.time() < 7400
    assert str(run.current("orchestrator")) == "claude_code:sonnet"
    run.call("orchestrator", brief="again", schema=Out)
    assert len(models["codex"].calls) == 1, "a benched model is not asked again"


def test_backups_in_order_and_the_last_one_waits(tmp_path, models):
    models["codex"].fails = RB.BackendUnavailable("cannot reach the server")
    models["claude_code"].fails = RB.BackendOutOfCredits("out of credits")
    run = _run(tmp_path, RoleConfig("codex", backup=("claude_code", "antigravity")))
    assert run.call("orchestrator", brief="go", schema=Out)[0].answer == "antigravity"
    last = [o for p, o in models["options"] if p == "antigravity"]
    assert last == [{}], "the last backup keeps its normal wait for credits"


def test_with_no_backup_nothing_changes(tmp_path, models):
    models["codex"].fails = RB.BackendOutOfCredits("out of credits")
    with pytest.raises(RB.BackendOutOfCredits):
        _run(tmp_path, RoleConfig("codex")).call("orchestrator", brief="go", schema=Out)
    assert models["options"] == [("codex", {})]


def test_a_conversation_moved_to_a_backup_is_handed_over(tmp_path, models):
    run = _run(tmp_path, RoleConfig("codex", backup=("claude_code",)))
    run.call("discussor", brief="hello", schema=Out, session="discussor")
    models["codex"].fails = RB.BackendOutOfCredits("out of credits")
    answer, session = run.call("discussor", brief="next", schema=Out, session="discussor",
                               takeover=lambda: "HANDOVER + next")
    assert answer.answer == "claude_code" and session == "discussor"
    assert models["claude_code"].calls[0]["prompt"] == "HANDOVER + next"
    assert (tmp_path / "rb" / "sessions" / "discussor-codex-001").is_dir(), "the old one is kept"
    # The callers' own check now says the conversation is on the backup's side.
    assert not run.takes_over("discussor", "discussor")


def test_without_a_takeover_the_old_turns_are_handed_over(tmp_path, models):
    run = _run(tmp_path, RoleConfig("codex", backup=("claude_code",)))
    run.call("orchestrator", brief="first brief", schema=Out, session="owner")
    models["codex"].fails = RB.BackendOutOfCredits("out of credits")
    run.call("orchestrator", brief="second brief", schema=Out, session="owner")
    sent = models["claude_code"].calls[0]["prompt"]
    assert "YOU TAKE OVER" in sent and "first brief" in sent and sent.endswith("second brief")


def test_backups_are_saved_and_checked(tmp_path):
    cfg = config.load(None, roles=["discussor=codex"])
    cfg = config.BootstrapConfig({**cfg.roles, "discussor": RoleConfig(
        "codex", backup=("codex_strata:strata", "claude_code:sonnet"))}, cfg.skills, cfg.skills_dir)
    config.save(tmp_path, cfg)
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["roles"]["discussor"]["backup"] == ["codex_strata:strata", "claude_code:sonnet"]
    assert config.load(tmp_path).roles["discussor"].backup == cfg.roles["discussor"].backup
    bad = {"discussor": RoleConfig("codex", backup=("nope:x",))}
    assert config.backup_problems(bad) == ["discussor: backup 'nope:x': no provider 'nope'"]


def test_a_model_at_capacity_moves_to_the_backup_for_a_while(tmp_path, models):
    models["codex"].fails = RB.BackendBusy("Selected model is at capacity. Please try a different model.")
    run = _run(tmp_path, RoleConfig("codex", backup=("claude_code",)))
    assert run.call("orchestrator", brief="go", schema=Out)[0].answer == "claude_code"
    assert 550 < run.benched["codex"] - time.time() < 650, "ten minutes, then the model again"


def test_at_capacity_is_a_busy_provider_not_a_failure():
    from research_backend.backends.codex import classify

    err = classify(RuntimeError("Selected model is at capacity. Please try a different model."))
    assert isinstance(err, RB.BackendBusy)


class Waiting(Model):
    """Out of credits: says so, then waits -- as codex.py and _cli.py do."""

    def run_structured(self, **kw):
        from research_backend.backends._progress import notice, wait_or_cancel

        notice(self, "Out of credits. Waiting 30 min (until 20:15).", until=time.time() + 1800)
        wait_or_cancel(1800, None, None)
        raise AssertionError("the wait should have been ended")


def test_a_waiting_call_says_so_and_pause_ends_the_wait(tmp_path, monkeypatch):
    import threading

    backend = Waiting("codex")
    monkeypatch.setattr("research_backend.conversation._backend", lambda *a, **k: backend)
    heard = []
    run = Run(tmp_path, roles={"orchestrator": RoleConfig("codex")}, quiet=True,
              listener=lambda kind, data: heard.append((kind, data)),
              interrupt=threading.Event())
    threading.Timer(0.5, run.interrupt.set).start()
    started = time.monotonic()
    with pytest.raises(RB.BackendCancelled, match="Paused"):
        run.call("orchestrator", brief="go", schema=Out)
    assert time.monotonic() - started < 5, "Pause ends a half-hour wait at once"
    notices = [d["snapshot"] for k, d in heard if k == "live" and "notice" in d["snapshot"]]
    assert notices and notices[0]["notice"].startswith("Out of credits") and notices[0]["until"]


def test_the_web_page_hears_of_the_wait(tmp_path):
    from research_agent.web import host as H
    from research_agent import state as S

    S.Store(tmp_path / "w").write(S.new())
    h = H.RunHost(tmp_path / "w", roles=["all=fake"], watch=False)
    seen = []
    h.bus.emit = lambda kind, **data: seen.append((kind, data))
    h._feed("call", {"state": "start", "label": "planner hypothesize", "model": "codex",
                     "session": "planner-001", "attempt": 1, "prompt": "p"})
    h._feed("live", {"session": "planner-001", "snapshot": {"notice": "Out of credits. Waiting 30 min.",
                                                              "until": 1.0}})
    assert ("note", {"text": "planner hypothesize: Out of credits. Waiting 30 min."}) in seen
    assert "waiting: Out of credits" in h.current
    assert h.running("planner")[0]["notice"].startswith("Out of credits")
