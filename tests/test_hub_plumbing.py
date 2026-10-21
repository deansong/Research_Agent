"""
WHAT:  The small pieces the orchestrator is built on: a command log's tail,
       how much a model can read, the orchestrator running where the
       discussor ran, and where one iteration's timeline ends.
WHY:   Each is a few lines, and each is a silent failure when wrong -- a
       validator shown no log, a prompt budget of zero, an orchestrator on a
       provider that cannot continue the discussor's conversation.

Run with:   python -m pytest tests/test_hub_plumbing.py -q
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent import activity  # noqa: E402


def test_log_tail_keeps_each_commands_end_and_its_exit_code(tmp_path):
    long = "\n".join(f"line {i}" for i in range(100))
    events = [
        {"kind": "command", "phase": "started", "command": "pytest -q"},
        {"kind": "command", "phase": "completed", "command": "pytest -q", "exit_code": 1,
         "output": long + "\nE   AssertionError: boom"},
        {"kind": "tool", "phase": "error", "command": "read_file", "exit_code": 1,
         "output": "path is not absolute"},
        {"kind": "reasoning", "phase": "completed", "summary": ["thinking"]},
    ]
    activity.write(str(tmp_path), "s1_code", events, sent={"prompt": "x"})
    tail = activity.log_tail(activity.latest_finished(str(tmp_path), "s1_code"), lines=5)
    assert "$ pytest -q   [exit 1]" in tail and "AssertionError: boom" in tail
    assert "earlier lines" in tail and "line 3\n" not in tail, "only the end of a long log"
    assert "$ read_file   [exit 1]" in tail and "thinking" not in tail
    assert activity.log_tail(None) == ""


def test_the_prompt_budget_follows_the_model():
    from agent.backends.models import context_window, prompt_budget_chars

    assert context_window("codex", "gpt-5.6-sol", reported=400_000) == 400_000
    assert context_window("claude_code", "opus") == 200_000
    assert context_window("antigravity", "gemini-4-argon-medium") == 1_000_000
    assert context_window("nobody", None) > 0
    # Far above the old 24,000-character cap: Opus reads ~400k characters.
    assert prompt_budget_chars("claude_code", "opus") == 400_000


def test_the_orchestrator_runs_where_the_discussor_does_unless_set():
    from agent.config import AgentConfig, BackendConfig, backend_for

    cfg = AgentConfig(default=BackendConfig(provider="codex", model="m"),
                      roles={"discussor": BackendConfig(provider="claude_code", model="opus")})
    assert backend_for(cfg, "orchestrator").provider == "claude_code"
    own = AgentConfig(default=cfg.default, roles={
        **cfg.roles, "orchestrator": BackendConfig(provider="antigravity", model="g")})
    assert backend_for(own, "orchestrator").provider == "antigravity"
    # Any other unconfigured role still takes the default.
    assert backend_for(cfg, "coder").provider == "codex"


def test_an_iteration_ends_at_a_supervisor_review_or_an_orchestrator_decision(tmp_path):
    from agent.supervisor import run_visits

    session = str(tmp_path)
    activity.record_visit(session, "s1", kind="agent")
    activity.record_visit(session, "supervisor", kind="supervisor")
    activity.record_visit(session, "s2", kind="agent")
    activity.record_visit(session, "orchestrator", kind="iteration_end")
    activity.record_visit(session, "s3", kind="agent")
    assert [v["node"] for v in run_visits(session)] == ["s3"]
