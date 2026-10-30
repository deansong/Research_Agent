"""
WHAT:  The command line on scripted answers: a conversation, the slash
       commands, leaving, and resuming.

Run with:   python -m pytest tests/test_cli.py -q
"""

from __future__ import annotations

import pytest

from research_agent import cli
from tests.scripted import script  # noqa: F401 -- fixture
from tests.test_graph import DESIGN, FINDINGS, decide, say


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
               say("The brief: compare LoRA ranks. Approve?", brief="Compare LoRA ranks."),
               say("Thanks, designing."), say("The design is ready. Approve?"),
               say("Thanks."), say("The bootstrap is done."))
    script.add("ResearcherOutput", FINDINGS)
    script.add("DesignOutput", DESIGN)
    script.add("OrchestratorOutput", decide("research", questions=["q"]), decide("brief"),
               decide("design"))
    _typing(monkeypatch, "/show brief", "/show nonsense", "/approve plan", "/approve",
            "/show hypotheses", "/approve", "/sessions", "/exit")
    run = tmp_path / "run"
    assert cli.main(["bootstrap", "Does LoRA rank matter?", "--run", str(run),
                     "--role", "all=fake"]) == 0
    out = capsys.readouterr().out
    assert f"Run: {run}" in out and "discussor=fake" in out
    assert "  . orchestrator -> research: because research" in out
    assert "  . researcher (research): 1 findings" in out
    assert "The brief: compare LoRA ranks. Approve?" in out and "Compare LoRA ranks." in out
    assert "The design is ready. Approve?" in out
    assert "- H1 [proposed] Higher LoRA rank helps on small data." in out, "/show"
    assert "Show which?" in out
    assert "Approve which? brief, design" in out
    assert "The bootstrap is done." in out
    assert "design (hypotheses, rubric, protocol, steps, nodes): approved" in out
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
    bad = say("ok", approve=["design"])                   # there is no design to approve
    script.add("DiscussorOutput", bad, bad, bad, say("Hello! What shall we study?"))
    _typing(monkeypatch, "hello?", "/show goal", "/retry", "/retry", "/exit")
    assert cli.main(["bootstrap", "idea", "--run", str(tmp_path / "r"), "--role", "all=fake"]) == 0
    out = capsys.readouterr().out
    assert "! discussor gave no usable answer in 3 attempts" in out
    assert "approve: there is no design yet" in out
    assert "The last step failed: /retry it first" in out
    assert "Hello! What shall we study?" in out
    assert "Nothing to retry." in out


def test_every_call_is_announced_and_trace_shows_input_and_output(tmp_path, script, monkeypatch,
                                                                  capsys):
    script.add("DiscussorOutput", say("Let me look.", ("research", "find baselines")),
               say("Found one."), say("Hi."), say("Hi again."))
    script.add("ResearcherOutput", FINDINGS)
    script.add("OrchestratorOutput", decide("research", questions=["q"]), decide("ask_user", "ok?"))
    _typing(monkeypatch)
    cli.main(["bootstrap", "idea one", "--run", str(tmp_path / "a"), "--role", "all=fake"])
    out = capsys.readouterr().out
    assert "saved as it goes: state/research.md" in out
    assert "  > discussor: asking fake (session discussor)..." in out
    assert "  > orchestrator: asking fake (session orchestrator-001)..." in out
    assert "  > researcher: asking fake (session researcher-001)..." in out
    assert "  < researcher: answered in" in out
    assert "INPUT" not in out, "without --trace, no prompts"

    _typing(monkeypatch)
    cli.main(["bootstrap", "idea two", "--run", str(tmp_path / "b"), "--role", "all=fake",
              "--trace"])
    out = capsys.readouterr().out
    assert out.count("---- discussor: standing instructions") == 1
    assert "You are the DISCUSSOR" in out
    assert "---- discussor: INPUT" in out and "=== THE USER SAYS ===\nidea two" in out
    assert "[+ skills" not in out, "no skills by default: nothing is discovered"
    assert "---- discussor: OUTPUT (" in out and '"reply": "Hi."' in out


def test_trace_can_be_switched_during_a_run(tmp_path, script, monkeypatch, capsys):
    script.add("DiscussorOutput", say("Hi."), say("Sure."))
    _typing(monkeypatch, "/trace maybe", "/trace on", "go on", "/exit")
    cli.main(["bootstrap", "idea", "--run", str(tmp_path / "r"), "--role", "all=fake"])
    out = capsys.readouterr().out
    assert "/trace on or /trace off (it is off)." in out and "Tracing is on." in out
    assert out.count("---- discussor: INPUT") == 1, "only the call after /trace on"
    assert "=== THE USER SAYS ===\ngo on" in out


def test_execute_runs_the_approved_design_with_commands(tmp_path, script, monkeypatch, capsys):
    import json

    from research_agent import state as S
    from tests.test_execute_graph import PAPER, REVIEW, reviewed, approved_state, done_writing
    from tests.test_execute_graph import decide as move, say as tell, verdict

    run = tmp_path / "run"
    S.Store(run).write(approved_state())
    (run / "config.json").write_text(json.dumps({"roles": {"discussor": {"provider": "fake"}}}))
    (run / "execute.json").write_text(json.dumps({"execute": {"poll_seconds": 0.05}}))

    class Folder:                     # done_writing() needs only artifacts()
        @staticmethod
        def artifacts(step_id):
            return run / "artifacts" / f"step_{int(step_id):02d}"

    script.add("ExecDecision", move("run", "1"), move("run", "2"), move("run", "2"), move("done"))
    script.add("PaperReport", PAPER)
    reviewed(script)
    script.add("StepReport", done_writing(Folder, "1"), done_writing(Folder, "2"))
    script.add("Verdict", verdict(), verdict())
    script.add("ExecDiscussorOutput", tell("Step 1 passed. Step 2 is next; go?"),
               tell("Step 2 passed: all done."))
    _typing(monkeypatch, "/steps", "/go", "/jobs", "/budget", "/logs 2", "/exit")
    assert cli.main(["execute", "--run", str(run), "--role", "all=fake"]) == 0
    out = capsys.readouterr().out
    assert "Nodes: builder (steps 1; full access), trainer (steps 2; full access)" in out
    assert "  . orchestrator -> run 1: because run 1" in out
    assert "  . step 1 verdict -- pass: pass it is" in out
    assert "step 2 waits for your go (/go)" in out
    assert "1. [pass] Data (builder, 1 attempts" in out, "/steps"
    assert "Step 2 passed: all done." in out and "Done: every step passed" in out
    assert "(no jobs)" in out and "0.00 GPU-hours used (no limit)" in out
    assert "No job or step 2 with jobs." in out
    saved = json.loads((run / "execute.json").read_text())
    assert saved["roles"]["worker"]["provider"] == "fake" and saved["execute"]["poll_seconds"] == 0.05


def test_execute_refuses_a_run_that_is_not_approved(tmp_path, capsys):
    from research_agent import state as S

    S.Store(tmp_path).write(S.new())
    with pytest.raises(SystemExit):
        cli.main(["execute", "--run", str(tmp_path), "--role", "all=fake"])
    assert "the design is not approved" in capsys.readouterr().err


def test_execute_answers_while_the_team_works(tmp_path, script, monkeypatch, capsys):
    """With a terminal, a line typed while the team works is answered on the
    side, and the work goes on."""
    import threading
    import time

    from research_agent import state as S
    from research_agent.bootstrap.config import RoleConfig
    from research_agent.execute import config as exec_config, nodes as exec_nodes
    from research_agent.execute.session import Execution
    from tests.test_execute_graph import approved_state, done_writing
    from tests.test_execute_graph import decide as move, say as tell, verdict

    run_dir = tmp_path / "run"
    S.Store(run_dir).write(approved_state())
    roles = {r: RoleConfig("fake") for r in (*exec_config.ROLES, "discussor")}
    run = exec_nodes.ExecRun(run_dir, roles, skills={}, quiet=True,
                             limits=exec_config.defaults())
    working, answered, settled = threading.Event(), threading.Event(), threading.Event()

    def owner(prompt):
        working.set()
        assert answered.wait(10), "the side answer came while the step was working"
        return done_writing(run, "1")(prompt)

    def side(prompt):
        answered.set()
        return {"reply": "Step 1 is counting the rows."}

    def last_words(prompt):
        settled.set()
        return tell("Step 1 passed. Anything else?")

    script.add("ExecDecision", move("run", "1"), move("ask_user", task="Anything else?"))
    script.add("StepReport", owner)
    script.add("Verdict", verdict())
    script.add("AsideOutput", side)
    script.add("ExecDiscussorOutput", last_words)
    lines = iter([(working, "how is step 1 going?"), (settled, "/exit")])

    def typed(prompt=""):
        try:
            event, line = next(lines)
        except StopIteration:
            raise EOFError
        assert event.wait(10)
        time.sleep(0.2)                  # let the graph get where it is going
        return line

    monkeypatch.setattr("builtins.input", typed)
    assert cli._converse_exec(Execution(run), exec_config.defaults(), concurrent=True) == 0
    out = capsys.readouterr().out
    assert "You can write at any time" in out
    assert "answering on the side; the team works on" in out
    assert "Step 1 is counting the rows." in out and "Step 1 passed. Anything else?" in out
    assert out.index("Step 1 is counting the rows.") < out.index("Step 1 passed.")
