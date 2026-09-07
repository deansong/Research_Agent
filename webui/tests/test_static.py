"""
WHAT:  Checks the browser-side files without a browser.
WHY:   There is no node in this environment and no build step by design, so
       nothing would otherwise catch a stray bracket in app.js -- and the
       symptom of one is a completely blank page with an error only visible in
       a console nobody has open.
CONCEPT: Parse the JavaScript with tree-sitter; parse the HTML with the stdlib.

These are cheap, structural checks. They cannot tell you the UI looks right --
only that it will load at all, which is the failure mode worth automating.

Run with:   python -m pytest webui/tests -q
"""

from __future__ import annotations

import pathlib
import re
import sys
from html.parser import HTMLParser

import pytest

STATIC = pathlib.Path(__file__).resolve().parents[1] / "static"

#: Elements the HTML spec says never have a closing tag.
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"}


def _js_files() -> list[pathlib.Path]:
    """Our own modules only -- vendored libraries are somebody else's problem."""
    return sorted(p for p in (STATIC / "js").glob("*.js"))


def _parser():
    try:
        import tree_sitter_javascript as ts_js
        from tree_sitter import Language, Parser
    except ImportError:  # pragma: no cover - the check is optional
        pytest.skip("tree-sitter-javascript not installed; skipping the JS parse")
    return Parser(Language(ts_js.language()))


def test_javascript_parses():
    """Every module we wrote is syntactically valid.

    tree-sitter is error-tolerant by design: it always returns a tree, and marks
    the parts it could not understand as ERROR or MISSING nodes. So the check is
    "are there any of those", not "did it throw".
    """
    parser = _parser()
    files = _js_files()
    assert files, f"no JS modules found under {STATIC / 'js'}"

    for path in files:
        tree = parser.parse(path.read_bytes())
        bad = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "ERROR" or node.is_missing:
                line = node.start_point[0] + 1
                bad.append(f"{path.name}:{line} {node.type}")
            stack.extend(node.children)
        assert not bad, "syntax errors: " + ", ".join(bad[:5])

    print(f"PASS  {len(files)} JS modules parse cleanly")


def test_module_imports_resolve():
    """Every relative import points at a file that exists.

    Native ES modules resolve at load time in the browser, so a renamed file is
    a 404 and a dead page -- with no build step to catch it first.
    """
    pattern = re.compile(r"""^\s*import\s+(?:.+?\s+from\s+)?['"](\.[^'"]+)['"]""",
                         re.MULTILINE)
    for path in _js_files():
        for target in pattern.findall(path.read_text()):
            resolved = (path.parent / target).resolve()
            assert resolved.exists(), f"{path.name} imports missing {target}"
    print("PASS  every relative import resolves to a real file")


def test_html_references_exist_and_tags_balance():
    """The page's own links resolve, and its tags are balanced.

    An unbalanced tag does not stop the page loading -- browsers recover -- but
    it silently reparents whole panels, which looks like a CSS bug and is not.
    """
    html = (STATIC / "index.html").read_text()

    for match in re.finditer(r'(?:src|href)="(/[^"]+)"', html):
        target = STATIC / match.group(1).lstrip("/")
        assert target.exists(), f"index.html references missing {match.group(1)}"

    class Balance(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack: list[str] = []
            self.errors: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag not in VOID:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag in VOID:
                return
            if not self.stack:
                self.errors.append(f"</{tag}> with nothing open")
            elif self.stack[-1] != tag:
                self.errors.append(f"</{tag}> closes <{self.stack[-1]}>")
            else:
                self.stack.pop()

    balance = Balance()
    balance.feed(html)
    assert not balance.errors, balance.errors
    assert not balance.stack, f"never closed: {balance.stack}"
    print("PASS  index.html is balanced and every asset it names exists")


def test_ids_used_by_js_exist_in_the_html():
    """getElementById on a typo returns null, and the failure is a TypeError
    thrown halfway through startup -- after some listeners are wired and some
    are not, which is a genuinely confusing state to debug."""
    html = (STATIC / "index.html").read_text()
    present = set(re.findall(r'id="([^"]+)"', html))

    wanted: set[str] = set()
    for path in _js_files():
        wanted |= set(re.findall(r"""\bel\(['"]([a-z0-9-]+)['"]\)""", path.read_text()))
        wanted |= set(re.findall(
            r"""getElementById\(['"]([a-z0-9-]+)['"]\)""", path.read_text()))

    missing = sorted(wanted - present)
    assert not missing, f"JS looks up ids that index.html does not define: {missing}"
    print(f"PASS  all {len(wanted)} element ids the JS uses exist in the HTML")


def test_vendor_globals_exist():
    """The UMD bundles must still export the names our code reaches for.

    graph.js uses the bare globals `cytoscape` and `cytoscapeDagre`, because
    UMD bundles predate ES modules and there is no build step to import them.
    A version bump that renamed one would give a blank canvas and a console
    error nobody has open -- so pin it here instead.
    """
    vendor = STATIC / "vendor"
    files = {p.name: p.read_bytes() for p in vendor.glob("*.js")}
    assert files, "no vendored libraries found"

    for global_name, filename in [
        ("cytoscape", "cytoscape.min.js"),
        ("dagre", "dagre.min.js"),
        ("cytoscapeDagre", "cytoscape-dagre.min.js"),
    ]:
        assert filename in files, f"{filename} is missing from vendor/"
        # UMD assigns the global as `X.name=` or `.name=t()`; either way the
        # identifier appears next to an assignment in the wrapper.
        assert f".{global_name}=".encode() in files[filename], \
            f"{filename} no longer defines the global {global_name!r}"

    html = (STATIC / "index.html").read_text()
    for filename in files:
        assert filename in html, f"vendor/{filename} is never loaded by index.html"

    print(f"PASS  {len(files)} vendored bundles are loaded and export what we use")


def test_css_variables_are_defined_before_use():
    """A misspelt var() renders as nothing at all -- a transparent background or
    an invisible border -- with no error anywhere."""
    css = (STATIC / "css" / "app.css").read_text()
    defined = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", css, re.MULTILINE))
    used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
    missing = sorted(used - defined)
    assert not missing, f"CSS uses undefined custom properties: {missing}"
    print(f"PASS  all {len(used)} CSS variables are defined")


def test_classes_the_js_creates_are_styled():
    """An unstyled class is invisible rather than broken.

    The heartbeat expander is the case that prompted this: a `beat-more`
    button with no rule is a default grey browser button in the middle of a
    monospace log, and a `beat-detail` with no rule is an unbounded wall of
    text. Both "work" -- neither is usable -- and nothing anywhere complains.

    Deliberately a named list rather than every class scraped out of the JS:
    plenty of those are set for behaviour (`selected`, `dirty`) and are
    legitimately unstyled, so a scraped version would have to carry an
    exclusion list as long as the check.
    """
    css = (STATIC / "css" / "app.css").read_text()
    for name in [
        "logline", "heartbeat", "beat-head", "beat-text", "beat-more",
        "beat-detail", "beat-detail-head", "beat-event",
    ]:
        assert re.search(rf"\.{re.escape(name)}\b", css), f"no CSS rule for .{name}"
    print("PASS  every class the heartbeat expander creates has a rule")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
