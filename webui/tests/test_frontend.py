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
