"""
WHAT:  The command line on scripted answers: a conversation, the slash
       commands, leaving, and resuming.

Run with:   python -m pytest tests/test_cli.py -q
"""

from __future__ import annotations

import pytest

from research_agent import cli
from tests.scripted import script  # noqa: F401 -- fixture
from tests.test_graph import FINDINGS, HYPO, decide, say


def _typing(monkeypatch, *lines):
    queue = list(lines)

    def fake_input(prompt=""):
        if not queue:
            raise EOFError
        line = queue.pop(0)
        print(prompt + line)
        return line

    monkeypatch.setattr("builtins.input", fake_input)


def test_a_conversation_with_commands(tmp_path, script, monkeypatch, capsys):
    script.add("DiscussorOutput",
               say("Let me check the literature.", ("research", "baselines")),
               say("H1, R1 and the protocol are ready. Approve?"),
               say("Thanks."), say("Shall I write the plan?"))
    script.add("ResearcherOutput", FINDINGS)
    script.add("HypothesizeOutput", HYPO)
    script.add("OrchestratorOutput", decide("research", questions=["q"]), decide("hypothesize"),
               decide("ask_user", "Approve?"), decide("ask_user", "Plan now?"))
    _typing(monkeypatch, "/show hypotheses", "/show nonsense", "/approve plan",
            "/approve hypotheses rubric protocol", "/sessions", "/exit")
    run = tmp_path / "run"
    assert cli.main(["bootstrap", "Does LoRA rank matter?", "--run", str(run),
                     "--role", "all=fake"]) == 0
    out = capsys.readouterr().out
    assert f"Run: {run}" in out and "discussor=fake" in out
    assert "  . orchestrator -> research: because research" in out
    assert "  . researcher (research): 1 findings" in out
    assert "H1, R1 and the protocol are ready. Approve?" in out
    assert "- H1 [proposed] Rank matters" in out, "/show"
    assert "Show which?" in out
    assert "There is no plan to approve yet." in out
    assert "Shall I write the plan?" in out
    assert "hypotheses: approved | rubric: approved | protocol: approved" in out
    assert "orchestrator-001" in out and "discussor" in out, "/sessions"
    assert f"Finished. The run is in {run}." in out
    assert (run / "config.json").exists() and (run / "state" / "research.md").exists()


def test_a_run_can_be_left_and_resumed(tmp_path, script, monkeypatch, capsys):
    script.add("DiscussorOutput", say("What is your budget?"), say("One GPU it is."))
    run = tmp_path / "run"
    _typing(monkeypatch)                                    # leaves at the first prompt
    cli.main(["bootstrap", "idea", "--run", str(run), "--role", "all=fake"])
    assert "Carry on with:  python -m research_agent bootstrap --resume" in capsys.readouterr().out

    _typing(monkeypatch, "one GPU", "/exit")
    assert cli.main(["bootstrap", "--resume", str(run)]) == 0
    out = capsys.readouterr().out
    assert "What is your budget?" in out and "One GPU it is." in out


def test_bad_arguments_are_refused(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.main(["bootstrap"])
    with pytest.raises(SystemExit):
        cli.main(["bootstrap", "--resume", str(tmp_path)])
    with pytest.raises(SystemExit):
        cli.main(["bootstrap", "idea", "--run", str(tmp_path / "r"), "--role", "chef=fake"])
    assert "No role 'chef'" in capsys.readouterr().err


def test_a_failed_step_waits_for_retry(tmp_path, script, monkeypatch, capsys):
    bad = say("ok", approve=["plan"])                     # there is no plan to approve
    script.add("DiscussorOutput", bad, bad, bad, say("Hello! What shall we study?"))
    _typing(monkeypatch, "hello?", "/show goal", "/retry", "/retry", "/exit")
    assert cli.main(["bootstrap", "idea", "--run", str(tmp_path / "r"), "--role", "all=fake"]) == 0
    out = capsys.readouterr().out
    assert "! discussor gave no usable answer in 3 attempts" in out
    assert "approve: there is no plan yet" in out
    assert "The last step failed: /retry it first" in out
    assert "Hello! What shall we study?" in out
    assert "Nothing to retry." in out
