"""
WHAT:  Runs the browser modules in a real JS engine against a stub DOM.
WHY:   There is no node here and no build step, so nothing else executes the
       front end at all -- and the failure mode of a runtime error at startup
       is a page where NOTHING responds, with the reason only in a console
       nobody has open. Exactly the bug this was written to chase.
CONCEPT: QuickJS plus about eighty lines of fake DOM.

This is emphatically not a browser. It cannot tell you the layout is wrong or
that a click does the right thing. It answers one question: does the module
graph LOAD and wire itself up without throwing? That is the question whose
"no" costs an afternoon.

Each module is wrapped in its own function so `const el = ...` in two files
does not collide -- concatenating ES modules is not the same as importing
them, and pretending otherwise produced a confusing SyntaxError before this
did the scoping properly.
"""

from __future__ import annotations

import json
import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parents[1] / "static"

#: module file -> the names it exports. Kept explicit rather than parsed,
#: because a wrong guess here shows up as a baffling ReferenceError in the
#: engine rather than as a problem with this list.
MODULES: list[tuple[str, list[str]]] = [
    ("api.js", ["Problem", "api", "subscribe"]),
    ("chat.js", ["Chat"]),
    ("graph.js", ["GraphPanel"]),
    ("plan.js", ["PlanPanel", "ownersByStep"]),
    ("inspector.js", ["Inspector", "renderActivity"]),
    ("topology.js", ["TopologyPanel"]),
    ("app.js", []),
]

_DOM = r"""
var __errors = [];
var __clicks = {};

function El(id) {
  this.id = id || '';
  this.classList = {
    _set: {},
    add(c) { this._set[c] = true },
    remove(c) { delete this._set[c] },
    toggle(c, on) { if (on === undefined) { this._set[c] = !this._set[c] } else if (on) { this._set[c] = true } else { delete this._set[c] } },
    contains(c) { return !!this._set[c] },
  };
  this.style = { setProperty() {} };
  this.dataset = {};
  this.disabled = false; this.value = ''; this.textContent = ''; this.innerHTML = '';
  this.title = ''; this.hidden = false; this.open = false; this.rows = 0;
  this.type = ''; this.checked = false; this.placeholder = ''; this.className = '';
  this.childElementCount = 0; this.firstElementChild = null;
  this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0;
  this.tagName = 'DIV';
  this._listeners = {};
  this._children = [];
}
// Appends are RECORDED, not ignored, so a test can ask "how many rows did
// this produce?" -- which is the only way to tell a line that updates itself
// from a line that repeats forty times.
El.prototype.appendChild = function (c) { this._children.push(c) };
El.prototype.append = function () {
  for (var i = 0; i < arguments.length; i++) this._children.push(arguments[i]);
  this.childElementCount = this._children.length;
};
El.prototype.replaceChildren = function () {
  this._children = [];
  this.childElementCount = 0;
};
El.prototype.remove = function () {};
El.prototype.addEventListener = function (type) {
  __clicks[this.id + ':' + type] = (__clicks[this.id + ':' + type] || 0) + 1;
};
El.prototype.removeEventListener = function () {};
El.prototype.setPointerCapture = function () {};
El.prototype.querySelector = function () { return null };
El.prototype.querySelectorAll = function () { return [] };
El.prototype.closest = function () { return null };
El.prototype.showModal = function () {};
El.prototype.close = function () {};
El.prototype.click = function () {};
El.prototype.focus = function () {};
Object.defineProperty(El.prototype, 'parentElement', {
  get() { return new El('parent') },
});

var __KNOWN = __IDS__;
var document = {
  documentElement: new El('root'),
  activeElement: null,
  getElementById(id) {
    if (__KNOWN.indexOf(id) === -1) {
      // The failure this harness exists to catch: every later line in the
      // same function is skipped, so half the app ends up unwired.
      __errors.push("getElementById('" + id + "') returned null");
      return null;
    }
    return new El(id);
  },
  createElement() { return new El('created') },
  createTextNode() { return new El('text') },
  addEventListener() {},
  querySelectorAll() { return [] },
  dispatchEvent() {},
};

var window = { innerWidth: 1600, addEventListener() {} };
var CustomEvent = function (type, opts) { this.type = type; this.detail = (opts || {}).detail };
var EventSource = function () {
  this.addEventListener = function () {}; this.close = function () {};
};
var fetch = function () {
  return Promise.resolve({ ok: true, status: 200, json() { return Promise.resolve({}) } });
};
var structuredClone = function (v) {
  return JSON.parse(JSON.stringify(v === undefined ? null : v));
};
var cytoscape = function () {
  return {
    on() {},
    nodes() { return { removeClass() {} } },
    elements() { return { addClass() {}, removeClass() {} } },
    getElementById() { return { addClass() {}, removeClass() {} } },
    resize() {}, fit() {},
  };
};
cytoscape.use = function () {};
var cytoscapeDagre = {};
var dagre = {};
var console = { log() {}, warn() {}, error() {} };
"""


def _element_ids() -> list[str]:
    html = (STATIC / "index.html").read_text()
    return sorted(set(re.findall(r'id="([^"]+)"', html)))


def _imported_names(text: str) -> list[str]:
    """The names a module actually imports, from its own import statements.

    Read rather than assumed, and that distinction cost a debugging round:
    injecting EVERY known name into every module shadowed each module's own
    declarations, so api.js -- which declares `Problem` and `api` itself --
    failed with "invalid redefinition of lexical identifier". A module must
    receive exactly what it asked for and nothing else.
    """
    names: list[str] = []
    for block in re.findall(r"import\s*\{([^}]*)\}\s*from", text):
        for part in block.split(","):
            name = part.strip().split(" as ")[-1].strip()
            if name:
                names.append(name)
    return names


def _module_source(name: str, exports: list[str]) -> str:
    """One module as a function returning its exports.

    A function per module rather than one concatenated script, because
    concatenating ES modules is not importing them: two files that each say
    `const el = ...` are fine as modules and a redefinition as one script.
    """
    text = (STATIC / "js" / name).read_text()
    imported = _imported_names(text)

    text = re.sub(r"^\s*import\s[\s\S]*?;\s*$", "", text, flags=re.M)
    text = re.sub(r"^\s*export\s*\{[\s\S]*?\};\s*$", "", text, flags=re.M)
    text = re.sub(r"^(\s*)export\s+", r"\1", text, flags=re.M)

    destructure = (f"  const {{ {', '.join(imported)} }} = __imports;\n"
                   if imported else "")
    returned = ", ".join(f"{n}: {n}" for n in exports)
    return (
        f"function __mod_{name.replace('.js', '')}(__imports) {{\n"
        f"{destructure}"
        f"{text}\n"
        f"  return {{ {returned} }};\n"
        f"}}\n"
    )


def build_script() -> str:
    dom = _DOM.replace("__IDS__", json.dumps(_element_ids()))
    parts = [dom]
    for name, exports in MODULES:
        parts.append(_module_source(name, exports))

    calls = ["var __ns = {};"]
    for name, exports in MODULES:
        stem = name.replace(".js", "")
        calls.append(f"Object.assign(__ns, __mod_{stem}(__ns));")
    parts.append("\n".join(calls))
    return "\n".join(parts)


def exercise(snippet: str, *, then: str | None = None):
    """Load the modules, run `snippet`, and return a JSON-able value.

    The load test above answers "does the app start?". This answers "does
    this one behaviour do what it claims?" -- against the real module source
    rather than a description of it.

    `then` exists because half the front end is async. An `await` inside a
    handler queues a microtask, and QuickJS runs the queue only when it is
    pumped -- so a snippet that reads its own result immediately reads the
    state from BEFORE the await, and the test fails for a reason that has
    nothing to do with the code. Pass the action as `snippet` and the reading
    as `then`, and the jobs are drained in between.
    """
    import quickjs

    ctx = quickjs.Context()
    ctx.eval(build_script())

    if then is None:
        return json.loads(ctx.eval(f"JSON.stringify((function () {{ {snippet} }})())"))

    ctx.eval(f"var __state = (function () {{ {snippet} }})();")
    # Bounded: a promise chain that never settles must fail the test, not hang
    # it. Nothing here legitimately needs more than a handful of turns.
    for _ in range(100):
        if not ctx.execute_pending_job():
            break
    return json.loads(ctx.eval(f"JSON.stringify((function () {{ {then} }})())"))


def run() -> tuple[str, list[str], dict]:
    """Load every module. Returns (error or "", null-lookups, listener counts)."""
    import quickjs

    ctx = quickjs.Context()
    error = ""
    try:
        ctx.eval(build_script())
    except Exception as exc:  # noqa: BLE001 -- the engine's message is the result
        error = str(exc)

    errors = json.loads(ctx.eval("JSON.stringify(__errors)"))
    clicks = json.loads(ctx.eval("JSON.stringify(__clicks)"))
    return error, errors, clicks


if __name__ == "__main__":
    error, nulls, clicks = run()
    print("load error:", error or "none")
    print("null lookups:", nulls or "none")
    print(f"listeners wired: {len(clicks)}")
    for key in sorted(clicks):
        print("   ", key)
