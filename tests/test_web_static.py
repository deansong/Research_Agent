"""
WHAT:  The web page's files, checked without a browser: every JS module
       parses and its imports exist, every element id the scripts look up is
       in the page they run on, every CSS variable is defined, every file the
       pages load is there.
WHY:   There is no build step to catch a typo; a missing id is a dead button
       that no Python test would see.

Run with:   python -m pytest tests/test_web_static.py -q
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "research_agent" / "web" / "static"
JS = STATIC / "js"
#: Which page each script runs on (ids are looked up there).
PAGES = {"app.js": "index.html", "chat.js": "index.html", "panels.js": "index.html",
         "statewin.js": "state.html", "node.js": "node.html"}


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.refs = set(), []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.add(a["id"])
        for key in ("src", "href"):
            if a.get(key, "").startswith("/"):
                self.refs.append(a[key])


def _page(name):
    page = _Page()
    page.feed((STATIC / name).read_text())
    return page


def test_every_module_parses_and_its_imports_exist():
    ts = pytest.importorskip("tree_sitter")
    tsjs = pytest.importorskip("tree_sitter_javascript")
    parser = ts.Parser(ts.Language(tsjs.language()))
    exports = {}
    for path in JS.glob("*.js"):
        text = path.read_text()
        tree = parser.parse(text.encode())
        assert not tree.root_node.has_error, f"{path.name} does not parse"
        exports[path.name] = set(re.findall(r"^export (?:async )?(?:function|class|const) (\w+)", text, re.M))
    for path in JS.glob("*.js"):
        for names, source in re.findall(r'^import \{([^}]+)\} from "\./([\w.]+)";', path.read_text(), re.M):
            assert source in exports, f"{path.name} imports {source}, which is not there"
            for name in (n.strip() for n in names.split(",")):
                assert name in exports[source], f"{path.name} imports {name} from {source}"


def test_every_id_a_script_looks_up_is_on_its_page():
    for script, page in PAGES.items():
        ids = _page(page).ids
        text = (JS / script).read_text()
        wanted = set(re.findall(r'(?:\$|getElementById)\("([\w-]+)"\)', text))
        assert wanted, script
        missing = wanted - ids
        assert not missing, f"{script} looks up {sorted(missing)}, not in {page}"
    # The research state view is on both pages.
    assert "state-view" in _page("index.html").ids and "state-view" in _page("state.html").ids


def test_what_the_pages_load_is_there():
    for page in ("index.html", "state.html", "node.html"):
        for ref in _page(page).refs:
            assert (STATIC / ref.lstrip("/")).exists(), f"{page} loads {ref}, which is missing"


def test_every_css_variable_is_defined():
    css = (STATIC / "css" / "app.css").read_text()
    defined = set(re.findall(r"(--[\w-]+)\s*:", css))
    used = set(re.findall(r"var\((--[\w-]+)\)", css))
    assert not used - defined, sorted(used - defined)


def test_the_markdown_escapes_html_first():
    text = (JS / "md.js").read_text()
    assert text.index("escapeHtml(text)") < text.index("<strong>")
    assert "innerHTML" not in (JS / "chat.js").read_text().replace('html: human ? markdown', "")
