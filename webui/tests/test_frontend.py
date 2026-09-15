"""
WHAT:  Loads the browser modules in a real JS engine and checks they wire up.
WHY:   With no node and no build step, nothing else EXECUTES the front end.
       tree-sitter proves the syntax parses; it cannot catch a null lookup or a
       call to something that is not a function -- and the symptom of either is
       a page where nothing at all responds, with the reason in a console
       nobody has open. That exact report is why this file exists.
CONCEPT: QuickJS plus a stub DOM. See jsdom_harness.py.

What it cannot do: tell you the layout is right, or that a click does the
right thing. It answers the one question whose "no" costs an afternoon --
does the app start?

Run with:   python -m pytest webui/tests/test_frontend.py -q
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


def _run():
    try:
        import quickjs  # noqa: F401
    except ImportError:  # pragma: no cover - the check is optional
        pytest.skip("quickjs not installed; skipping the frontend execution test")
    from jsdom_harness import run

    return run()


def test_the_app_starts_without_throwing():
    error, nulls, _ = _run()
    assert not error, f"the front end throws at load: {error}"
    # A null from getElementById does not throw by itself -- it throws on the
    # next line, halfway through wiring, so some listeners are attached and
    # some are not. Worse than a clean failure, because the page half works.
    assert not nulls, f"element lookups returned null: {nulls}"
    print("PASS  every module loads and every element lookup resolves")


def test_every_control_gets_its_listener():
    """The specific failure this catches: a control that renders and does
    nothing, because the line that would have wired it never ran.

    Named individually rather than counted, so adding a button does not
    silently satisfy an assertion about a total.
    """
    error, _, listeners = _run()
    assert not error, error

    for control, event in [
        ("btn-new", "click"),
        ("btn-open", "click"),
        ("btn-auth", "click"),
        ("btn-auth-cancel", "click"),
        ("btn-auth-submit", "click"),
        ("auth-flow-input", "keydown"),
        ("btn-pause", "click"),
        ("btn-stop", "click"),
        ("btn-scroll", "click"),
        ("btn-json-save", "click"),
        ("composer", "submit"),
        ("composer-input", "keydown"),
        ("form-new", "submit"),
        ("new-task", "input"),
        ("canvas-tabs", "click"),
        ("inspector-tabs", "click"),
    ]:
        key = f"{control}:{event}"
        assert key in listeners, f"nothing listens for {key}"
    print(f"PASS  all {len(listeners)} listeners wired, including pause and stop")


def test_a_long_turn_makes_one_row_not_forty():
    """The heartbeat line must replace itself, not accumulate.

    A twenty-minute turn prints one of these every thirty seconds. Appended,
    that is forty near-identical rows shoving the conversation off the screen;
    replaced, it is one line that stays current. The difference is invisible
    until you watch a real turn, so it is asserted here instead.
    """
    try:
        import quickjs  # noqa: F401
    except ImportError:  # pragma: no cover
        pytest.skip("quickjs not installed")
    from jsdom_harness import exercise

    result = exercise("""
      var chat = new __ns.Chat({ onSend() {}, onDetail() {} });
      chat.logLine('    ... working: 30s \u00b7 4 reasoning \u00b7 quiet 12s');
      chat.logLine('    ... working: 60s \u00b7 9 command \u00b7 quiet 3s');
      chat.logLine('    ... working: 90s \u00b7 21 command \u00b7 quiet 1s');
      chat.logLine('[executor] done');
      return {
        rows: chat.log._children.length,
        shown: chat.beatText.textContent,
        expander: chat.beatButton.textContent,
        collapsed: chat.beatDetail.hidden,
      };
    """)

    # Three heartbeats and one ordinary line -> two rows, not four.
    assert result["rows"] == 2, result
    assert result["shown"] == "working: 90s \u00b7 21 command \u00b7 quiet 1s", result
    assert result["expander"] == "show detail"
    assert result["collapsed"] is True
    print("PASS  three heartbeats collapse into one row, expander closed")


def test_the_expander_shows_the_turns_events():
    """What the row expands INTO: the events the summary was counting.

    The user's complaint verbatim was that "313 events" was reported and the
    313 events were not. So this asserts the commands and the reasoning come
    back out of the expander, not just that it opens.
    """
    try:
        import quickjs  # noqa: F401
    except ImportError:  # pragma: no cover
        pytest.skip("quickjs not installed")
    from jsdom_harness import exercise

    result = exercise("""
      var turn = {
        running: true,
        turn: {
          node: 'evidence_design', index: 3,
          counts: { command: 2, reasoning: 1 },
          progress: { elapsed: 310, streamed: 16608,
                      live: { message: '{"task_brief": "Compare hotel' } },
          events: [
            { kind: 'reasoning', at: 4, summary: ['reading the repository'] },
            { kind: 'command', phase: 'completed', at: 9,
              command: 'pytest -q', exit_code: 1 },
            { kind: 'file_change', at: 12, changes: [{ path: 'docs/study.md' }] },
          ],
        },
      };
      var chat = new __ns.Chat({ onSend() {}, onDetail() { return turn } });
      chat.logLine('    ... working: 30s \u00b7 3 events \u00b7 quiet 2s');
      chat.toggleDetail();
      return chat;
    """, then="""
      var chat = __state;
      return {
        open: !chat.beatDetail.hidden,
        expander: chat.beatButton.textContent,
        rendered: chat.beatDetail._children.map(function (c) { return c.textContent }),
      };
    """)

    assert result["open"] is True
    assert result["expander"] == "hide detail"
    rendered = " | ".join(result["rendered"])
    assert "evidence_design" in rendered and "turn 3" in rendered
    assert "pytest -q" in rendered and "exit 1" in rendered
    assert "reading the repository" in rendered
    assert "docs/study.md" in rendered
    # The live tail, and the figure that explains a quiet expander.
    assert '{"task_brief": "Compare hotel' in rendered, rendered
    assert "writing 16.6k" in rendered, rendered
    print("PASS  the expander renders the turn's actual commands and reasoning")


def test_the_plan_editor_offers_a_check_and_a_gate_per_step():
    """The plan's two new fields have to be reachable in the browser.

    They are the two a person is best placed to fix: the planner writes a
    check from what it can infer, but you know the command that actually
    decides, and you know which steps you want to be asked about before they
    run. A field that only exists in the JSON is a field nobody edits.
    """
    try:
        import quickjs  # noqa: F401
    except ImportError:  # pragma: no cover
        pytest.skip("quickjs not installed")
    from jsdom_harness import exercise

    result = exercise("""
      var panel = new __ns.PlanPanel(document.getElementById('tab-plan'),
                                     { onSave() {}, onSelectStep() {} });
      panel.render({ summary: 's', steps: [
        { id: '3', title: 'Write the sweep', check: 'pytest passes', gate: false },
        { id: '4', title: 'Launch it', check: '8 result files', gate: true },
      ] }, new Map());

      // Walk what was actually built, rather than trusting a call count.
      var found = { checks: [], gates: 0 };
      (function walk(node) {
        for (var i = 0; i < (node._children || []).length; i++) {
          var child = node._children[i];
          if (child.className === 'plan-check') found.checks.push(child.value);
          if (child.type === 'checkbox' && child.checked) found.gates++;
          walk(child);
        }
      })(panel.container);
      return found;
    """)

    assert result["checks"] == ["pytest passes", "8 result files"], result
    assert result["gates"] == 1, "only the gated step's box is ticked"
    print("PASS  every step gets an editable check, and the gate reflects it")


def test_the_final_answer_is_shown_rather_than_labelled_away():
    """It was rendered as "(final answer)" and nothing else.

    The rationale was that it duplicates the normal return path -- true for
    the designer, whose answer becomes graph.json two tabs away, and false for
    every other node, whose answer becomes state you cannot read anywhere in
    the UI. Either way, hiding the one artefact a turn produced is the wrong
    default: a person looking at a node's activity is looking for what it
    SAID.
    """
    try:
        import quickjs  # noqa: F401
    except ImportError:  # pragma: no cover
        pytest.skip("quickjs not installed")
    from jsdom_harness import exercise

    result = exercise("""
      var answer = JSON.stringify({ task_brief: "Compare skill associations",
                                    graph: { entry: "audit" } });
      var events = [{ kind: 'message', phase: 'completed', at: 861,
                      text: answer, is_final_json: true }];
      var host = document.getElementById('tab-activity');
      __ns.renderActivity(host, { turns: [
        { node: 'designer', index: 2, started: '', counts: { message: 1 },
          events: events },
      ] });

      var found = [];
      (function walk(node) {
        for (var i = 0; i < (node._children || []).length; i++) {
          var child = node._children[i];
          if (child.className === 'event-output') found.push(child.textContent);
          if (child.className === 'event-label') found.push(child.textContent);
          walk(child);
        }
      })(host);
      return found;
    """)

    joined = "\n".join(result)
    # The label still says it is THE answer, and now says how big.
    assert "the final answer" in joined, joined[:200]
    assert "chars" in joined, "say the size, so an unexpanded row is legible"
    # And the body is there, pretty-printed rather than a one-line blob.
    assert "task_brief" in joined and "Compare skill associations" in joined, joined[:300]
    assert '"graph"' in joined, "the whole document, not the first key"
    print("PASS  a node's final answer is readable in the activity panel")


def test_the_harness_would_notice_a_broken_lookup():
    """A test that cannot fail is worse than no test.

    The DOM stub records a null rather than throwing, so if that recording
    stopped working every other assertion here would pass vacuously.
    """
    import json

    import quickjs

    from jsdom_harness import build_script

    ctx = quickjs.Context()
    ctx.eval(build_script())
    ctx.eval("document.getElementById('definitely-not-a-real-id')")
    recorded = json.loads(ctx.eval("JSON.stringify(__errors)"))
    assert any("definitely-not-a-real-id" in e for e in recorded), recorded
    print("PASS  the harness really does record a failed lookup")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_the_login_panel_says_which_of_three_states_each_provider_is_in():
    """Three, not two. "Could not tell" rendered as "signed out" sends somebody
    to re-run a login that is working -- the same three-valued rule the probe
    keeps, which is worth nothing if the page flattens it back to a boolean.

    Also asserts the row for a provider that cannot be signed in from a browser
    offers the COMMAND instead of a button, because a button that cannot work
    is worse than a sentence.
    """
    try:
        import quickjs  # noqa: F401
    except ImportError:  # pragma: no cover
        pytest.skip("quickjs not installed")
    from jsdom_harness import exercise

    result = exercise("""
      var panel = new __ns.AuthPanel({ getRepo: function () { return '/tmp/x' } });
      panel.canLogin = true;
      panel.providers = [
        { provider: 'codex', installed: true, logged_in: true, ok: true,
          in_use: true, detail: 'Logged in using ChatGPT', method: 'ChatGPT',
          account: '', browser_login: true, login_command: 'codex login', note: '' },
        { provider: 'claude_code', installed: true, logged_in: false, ok: false,
          in_use: true, detail: 'Not logged in.', method: '', account: '',
          browser_login: true, login_command: 'claude auth login', note: '' },
        { provider: 'antigravity', installed: true, logged_in: null, ok: false,
          in_use: false, detail: 'agy models exited 9', method: '', account: '',
          browser_login: false, login_command: 'agy',
          note: 'agy signs in through its full-screen interface, so this one has to be done in a terminal.' },
      ];
      panel.render();

      function read(row) {
        var head = row._children[0];
        var action = row._children[2];
        var kinds = action._children.map(function (c) { return c.className });
        return {
          badge: head._children[1].textContent,
          inuse: head._children.length > 2,
          klass: row.className,
          actions: kinds,
          actionText: action._children.map(function (c) { return c.textContent }),
        };
      }

      var rows = document.getElementById('auth-list')._children;
      return { count: rows.length, rows: rows.map(read) };
    """)

    assert result["count"] == 3, result
    codex, claude, agy = result["rows"]

    assert codex["badge"] == "signed in"
    assert "ok" in codex["klass"]
    assert codex["inuse"] is True

    assert claude["badge"] == "signed out"
    assert "bad" in claude["klass"]
    assert any("btn" in k for k in claude["actions"]), "no way to fix it"

    assert agy["badge"] == "unknown", "a failed probe must not read as signed out"
    assert "unknown" in agy["klass"]
    assert not any("btn" in k for k in agy["actions"]), (
        "offered a sign-in button for a CLI that cannot do it from a browser")
    assert "agy" in " ".join(agy["actionText"]), "did not say what to run instead"
    print("PASS  three states rendered, and agy shows a command not a button")


def test_the_login_panel_shows_the_link_and_the_code():
    """The two things a human has to act on. The transcript underneath is
    context; these are the task."""
    try:
        import quickjs  # noqa: F401
    except ImportError:  # pragma: no cover
        pytest.skip("quickjs not installed")
    from jsdom_harness import exercise

    result = exercise("""
      var panel = new __ns.AuthPanel({ getRepo: function () { return '' } });
      panel.showFlow('codex');
      panel.paint({
        provider: 'codex', state: 'running', running: true, error: '',
        url: 'https://auth.openai.com/codex/device', code: 'CW4P-ZJ3G9',
        accepts_input: false, input_label: '', output: 'Welcome to Codex',
      });
      var before = {
        url: document.getElementById('auth-flow-url').href,
        code: document.getElementById('auth-flow-code').textContent,
        codeHidden: document.getElementById('auth-flow-code-step').hidden,
        inputHidden: document.getElementById('auth-flow-input-row').hidden,
        cancelHidden: document.getElementById('btn-auth-cancel').hidden,
      };

      panel.paint({
        provider: 'claude_code', state: 'failed', running: false,
        error: 'Not logged in.', url: 'https://claude.com/oauth', code: '',
        accepts_input: false, input_label: 'Paste the code',
        output: 'Invalid code.',
      });
      var after = {
        error: document.getElementById('auth-flow-error').textContent,
        errorHidden: document.getElementById('auth-flow-error').hidden,
        cancelHidden: document.getElementById('btn-auth-cancel').hidden,
      };
      return { before: before, after: after };
    """)

    before, after = result["before"], result["after"]
    assert before["url"] == "https://auth.openai.com/codex/device"
    assert before["code"] == "CW4P-ZJ3G9"
    assert before["codeHidden"] is False
    assert before["inputHidden"] is True, "asked for a code the flow does not want"
    assert before["cancelHidden"] is False

    assert after["errorHidden"] is False and after["error"] == "Not logged in."
    assert after["cancelHidden"] is True, "offered to cancel something already over"
    print("PASS  link and code shown, and a finished flow stops offering Cancel")


def test_the_node_summary_names_the_model_not_just_the_role():
    """The complaint this fixes: the page showed a node ran on "coder" and
    nothing anywhere said whether that was a flagship or the cheap tier.

    For a checker that is the difference between a verifier and a rubber
    stamp, and it is the whole reason roles are separate from models.
    """
    try:
        import quickjs  # noqa: F401
    except ImportError:  # pragma: no cover
        pytest.skip("quickjs not installed")
    from jsdom_harness import exercise

    result = exercise("""
      var inspector = new __ns.Inspector({ onSave() {}, onSelectSteps() {} });
      inspector.showContext({
        name: 'run_method', kind: 'agent', backend: 'runner', access: 'write',
        steps: ['3.1'],
        step_titles: ['3.1 LoRA r=16, seeds 0-2'],
        output: [{name: 'results_path'}, {name: 'summary'}],
        resolved: {provider: 'antigravity', model: 'gemini-3.8-flash-medium',
                   configured: false},
        prompts: {first: {template: 'x', rendered: 'run Qwen2.5-7B on sst2'},
                  next: {template: '', rendered: ''}},
        instructions: 'i', appended_instructions: '',
        my_steps: '3.1 our method', plan_outline: '1. Code',
        thread_key: {template: '', rendered: ''},
        last_output: {
          results_path: 'artifacts/lora16/seed0.json',
          summary: 'Qwen2.5-7B, sst2, lr 2e-4, 3 seeds',
        },
      });

      function text(el) {
        var out = el.textContent || '';
        for (var i = 0; i < el._children.length; i++) out += ' ' + text(el._children[i]);
        return out;
      }
      return { shown: text(document.getElementById('tab-context')) };
    """)

    shown = result["shown"]
    assert "runner" in shown, "the role is still named"
    assert "antigravity/gemini-3.8-flash-medium" in shown, \
        "the resolved model is not on the page"
    # A role nobody configured silently takes the default -- which is how an
    # experiment ends up on a model nobody chose.
    assert "falls back to the default" in shown, "an unconfigured role says so"
    # The step TITLE, not just its id: "steps 3.1" says nothing about what the
    # node is for, and the id is the only link back to the approved plan.
    assert "3.1 LoRA r=16, seeds 0-2" in shown, "the plan step title is missing"
    print("PASS  a node says which model it runs on, and whether anyone chose it")


def test_a_nodes_output_is_shown_as_fields_not_a_json_blob():
    """Where a research node's experiment actually lives -- which model, which
    dataset, which config. It used to be the last thing on the tab, JSON, under
    four sections of prompt."""
    try:
        import quickjs  # noqa: F401
    except ImportError:  # pragma: no cover
        pytest.skip("quickjs not installed")
    from jsdom_harness import exercise

    result = exercise("""
      var inspector = new __ns.Inspector({ onSave() {}, onSelectSteps() {} });
      inspector.showContext({
        name: 'design_experiments', kind: 'agent', backend: 'coder',
        access: 'write', steps: ['2.1'], output: [{name: 'summary'}],
        resolved: {provider: 'codex', model: 'gpt-5.6-sol', configured: true},
        prompts: {first: {template: 'x', rendered: 'y'},
                  next: {template: '', rendered: ''}},
        instructions: 'i', appended_instructions: '',
        my_steps: '', plan_outline: '',
        thread_key: {template: '', rendered: ''},
        last_output: {summary: 'Qwen2.5-7B on sst2, seeds 0-2', seeds: [0, 1, 2]},
      });
      function text(el) {
        var out = el.textContent || '';
        for (var i = 0; i < el._children.length; i++) out += ' ' + text(el._children[i]);
        return out;
      }
      return { shown: text(document.getElementById('tab-context')) };
    """)

    shown = result["shown"]
    assert "Qwen2.5-7B on sst2, seeds 0-2" in shown
    # Unquoted: a path or a config summary is what somebody is here to read,
    # and JSON.stringify would wrap it in quotes and escape it.
    assert '"Qwen2.5-7B' not in shown, "a string value was JSON-quoted"
    # Pretty-printed JSON indents with NEWLINES, so strip all whitespace.
    assert "[0,1,2]" in "".join(shown.split()), "a list still renders as JSON"
    print("PASS  a node's output reads as fields, with strings unquoted")
