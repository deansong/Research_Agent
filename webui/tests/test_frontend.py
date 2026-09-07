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
