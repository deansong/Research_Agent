"""
WHAT:  The design agent, shown and changed before the conversation starts.
WHY:   The feature is only real if an edit made on that tab is what the model
       is actually sent -- so the end-to-end test reads the turn record the
       discussor wrote, not the settings file.
CONCEPT: TestClient + `--backend fake`, plus the QuickJS harness for the panel.

Run with:   python -m pytest webui/tests/test_bootstrap_api.py -q
"""

from __future__ import annotations

import json
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from agent.bootstrap import settings  # noqa: E402
from webui import server  # noqa: E402

TIMEOUT = 20.0


class Args:
    backend = "fake"
    model = None
    backend_role = None
    config = None
    session = None
    supervisor = "off"


def _client(tmp: pathlib.Path) -> TestClient:
    server._RUNNERS.clear()
    return TestClient(server.create_app(tmp, Args()))


def _new(client, task="compare two tokenisers") -> dict:
    response = client.post("/api/sessions", json={"task": task, "start": False})
    assert response.status_code == 200, response.text
    return response.json()


def _wait_for_question(client, sid: str) -> dict:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        detail = client.get(f"/api/sessions/{sid}").json()
        if detail["waiting"]:
            return detail
        if not detail["busy"] and detail["phase"] == "finished":
            raise AssertionError(f"finished without asking: {detail['error']!r}")
        time.sleep(0.05)
    raise AssertionError("no question in time")


def test_a_session_created_without_starting_shows_its_design_agent(tmp_path):
    client = _client(tmp_path)
    detail = _new(client)
    assert detail["busy"] is False and detail["transcript"] == []

    view = client.get(f"/api/sessions/{detail['id']}/bootstrap").json()
    assert view["editable"] is True and view["started"] is False
    assert view["order"] == ["discussor", "researcher", "human", "planner", "designer",
                             "writer", "validator"]

    designer = view["nodes"]["designer"]
    assert designer["backend"]["provider"] == "fake"
    names = [p["name"] for p in designer["prompts"]]
    assert names == ["instructions", "reference", "repair_prefix"]
    reference = next(p for p in designer["prompts"] if p["name"] == "reference")
    assert "WHAT EACH NODE IS GIVEN" in reference["value"], "the real reference is shown"
    assert all(not p["modified"] for p in designer["prompts"])
    assert view["nodes"]["human"]["commands"], "the slash commands are listed"


def test_an_edited_prompt_is_what_the_discussor_is_actually_sent(tmp_path):
    client = _client(tmp_path)
    sid = _new(client)["id"]

    marker = "You are a TERSE discussor. Ask exactly one question. MARKER-7731."
    response = client.put(f"/api/sessions/{sid}/bootstrap", json={
        "nodes": {"discussor": {"prompts": {"instructions": marker}}}})
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["exists"] is True
    discussor = view["nodes"]["discussor"]
    assert discussor["prompts"][0]["modified"] is True

    stored = json.loads(pathlib.Path(view["path"]).read_text())
    assert stored["nodes"] == {"discussor": {"prompts": {"instructions": marker}}}, \
        "only the difference is written"

    assert client.post(f"/api/sessions/{sid}/start", json={}).status_code == 200
    _wait_for_question(client, sid)

    record = tmp_path / ".agent" / "sessions" / sid / "activity" / "discussor" / "0001.json"
    sent = json.loads(record.read_text())["sent"]
    # The edit, then the isolation rule every design role gets after it.
    assert sent["instructions"].startswith(marker)
    assert "SESSION ISOLATION" in sent["instructions"][len(marker):]


def test_editing_is_refused_while_running_and_allowed_once_paused(tmp_path):
    client = _client(tmp_path)
    sid = _new(client)["id"]
    client.post(f"/api/sessions/{sid}/start", json={})
    _wait_for_question(client, sid)

    view = client.get(f"/api/sessions/{sid}/bootstrap").json()
    assert view["editable"] is False and view["started"] is True
    refused = client.put(f"/api/sessions/{sid}/bootstrap",
                         json={"max_design_attempts": 5})
    assert refused.status_code == 409

    client.post(f"/api/sessions/{sid}/pause")
    deadline = time.monotonic() + TIMEOUT
    while client.get(f"/api/sessions/{sid}").json()["busy"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    ok = client.put(f"/api/sessions/{sid}/bootstrap", json={"max_design_attempts": 5})
    assert ok.status_code == 200, ok.text
    assert ok.json()["max_design_attempts"]["value"] == 5


def test_a_provider_that_cannot_run_read_only_is_refused_at_save(tmp_path):
    """agy cannot express read_only, and all three design roles are read_only.
    Found out here, not after pressing Start."""
    client = _client(tmp_path)
    sid = _new(client)["id"]
    response = client.put(f"/api/sessions/{sid}/bootstrap", json={
        "nodes": {"planner": {"provider": "antigravity", "model": "gemini-3.1-pro"}}})
    assert response.status_code == 422
    body = response.json()["detail"]
    assert body["where"] == "nodes.planner.provider"
    assert "read_only" in body["message"]


def test_bad_values_are_named(tmp_path):
    client = _client(tmp_path)
    sid = _new(client)["id"]
    cases = [
        ({"nodes": {"writer": {"model": "x"}}}, "nodes.writer"),
        ({"nodes": {"designer": {"options": {"nonsense": 1}}}}, "options.nonsense"),
        ({"nodes": {"designer": {"options": {"timeout": "soon"}}}}, "options.timeout"),
        ({"nodes": {"designer": {"prompts": {"instructions": "   "}}}},
         "nodes.designer.prompts.instructions"),
        ({"nodes": {"designer": {"prompts": {"persona": "x"}}}},
         "nodes.designer.prompts.persona"),
        ({"max_design_attempts": 0}, "max_design_attempts"),
        ({"surprise": 1}, "surprise"),
    ]
    for body, where in cases:
        response = client.put(f"/api/sessions/{sid}/bootstrap", json=body)
        assert response.status_code == 422, (body, response.text)
        assert response.json()["detail"]["where"] == where, (body, response.json())


def test_saving_the_defaults_back_is_a_reset(tmp_path):
    client = _client(tmp_path)
    sid = _new(client)["id"]
    view = client.put(f"/api/sessions/{sid}/bootstrap", json={
        "nodes": {"designer": {"prompts": {"repair_prefix": "Fix it."}}},
        "max_design_attempts": 4}).json()
    assert view["exists"] is True

    # What the panel's Default/Reset buttons send: full values equal to the
    # defaults. Nothing differs, so nothing is stored.
    defaults = {p["name"]: p["default"] for p in view["nodes"]["designer"]["prompts"]}
    view = client.put(f"/api/sessions/{sid}/bootstrap", json={
        "nodes": {"designer": {"provider": "fake", "options": {}, "prompts": defaults}},
        "max_design_attempts": 3}).json()
    assert view["exists"] is False

    client.put(f"/api/sessions/{sid}/bootstrap", json={"max_design_attempts": 6})
    assert client.delete(f"/api/sessions/{sid}/bootstrap").json()["exists"] is False


def test_options_come_from_the_backend_constructor_and_are_stored_as_a_diff(tmp_path):
    """Read from the constructor, so the form can never offer an option the
    backend does not take -- the fake takes none, codex takes six."""
    from agent.config import load_config

    assert settings.option_fields("fake") == []
    names = [f["name"] for f in settings.option_fields("codex")]
    assert {"timeout", "max_seconds", "busy_attempts"} <= set(names)
    claude = [f["name"] for f in settings.option_fields("claude_code")]
    assert {"timeout", "max_turns", "executable"} <= set(claude), claude

    cfg = load_config(repo_path=tmp_path, env={})
    parsed, _ = settings.save(tmp_path, cfg, {"nodes": {"designer": {
        "provider": "codex", "options": {"timeout": "900", "max_seconds": 7200}}}})
    assert parsed.nodes["designer"].options == {"timeout": 900.0}, \
        "an option equal to its default is not stored"
    with pytest.raises(settings.SettingsError) as caught:
        settings.save(tmp_path, cfg, {"nodes": {"designer": {"options": {"timeout": -1}}}})
    assert caught.value.where == "options.timeout"


def test_the_drawn_graph_matches_the_compiled_one():
    """graph_view is hand-written beside a hand-written graph. Every edge it
    draws between two real nodes must be one graph.py can actually take."""
    from agent.bootstrap.graph import build_bootstrap_graph
    from agent.config import load_config

    cfg = load_config(repo_path=pathlib.Path("/tmp"), env={}, cli=Args())

    class Stub:
        name = "stub"

    graph = build_bootstrap_graph({r: Stub() for r in settings.MODEL_NODES},
                                  paths=None, checkpointer=None, max_attempts=1,
                                  research=True)
    compiled = graph.get_graph()
    real = {(e.source, e.target) for e in compiled.edges}
    drawn = settings.graph_view(cfg)
    names = {n["id"] for n in drawn["nodes"]}
    assert names - {"__end__"} == set(settings.ALL_NODES)
    for edge in drawn["edges"]:
        assert (edge["from"], edge["to"]) in real, edge


def test_a_corrupt_settings_file_is_an_error_not_silent_defaults(tmp_path):
    (tmp_path / settings.FILE_NAME).write_text("{not json")
    with pytest.raises(settings.SettingsError):
        settings.load(tmp_path)
    (tmp_path / settings.FILE_NAME).write_text(json.dumps(
        {"nodes": {"validator": {"model": "x"}}}))
    with pytest.raises(settings.SettingsError):
        settings.load(tmp_path)


def test_switching_provider_without_a_model_does_not_inherit_the_old_one(tmp_path):
    """A model id belongs to its provider: codex's gpt-5.6-sol sent to claude
    fails every turn. Blank means the new provider's own default."""
    from agent.config import backend_for, load_config

    cfg = load_config(repo_path=tmp_path, env={})
    (tmp_path / settings.FILE_NAME).write_text(json.dumps(
        {"nodes": {"designer": {"provider": "claude_code", "model": None}}}))
    effective = settings.effective_config(cfg, settings.load(tmp_path))
    spec = backend_for(effective, "designer")
    assert spec.provider == "claude_code" and not spec.model


# ---------------------------------------------------------------------------
# the panel, in the JS engine
# ---------------------------------------------------------------------------

def test_the_panel_renders_a_real_view_and_round_trips_it(tmp_path):
    pytest.importorskip("quickjs")
    from webui.tests.jsdom_harness import exercise

    client = _client(tmp_path)
    sid = _new(client)["id"]
    view = client.get(f"/api/sessions/{sid}/bootstrap").json()

    result = exercise(f"""
      var box = document.createElement('div');
      var panel = new __ns.BootstrapPanel(box, {{}});
      panel.render({json.dumps(view)});
      var head = box._children[0];
      function texts(node, out) {{
        out = out || [];
        if (node && node.textContent) out.push(node.textContent);
        (node && node._children || []).forEach(function (c) {{ texts(c, out); }});
        return out;
      }}
      var all = texts(box).join(' | ');
      return {{
        dirty: panel.isDirty(),
        hasStart: all.indexOf('Start conversation') !== -1,
        nodes: Object.keys(panel.body().nodes),
        designerPrompts: Object.keys(panel.body().nodes.designer.prompts),
        attempts: panel.body().max_design_attempts,
        mentionsPath: all.indexOf('bootstrap.json') !== -1,
      }};
    """)
    assert result["dirty"] is False
    assert result["hasStart"] is True
    assert result["nodes"] == ["discussor", "researcher", "planner", "designer"]
    assert result["designerPrompts"] == ["instructions", "reference", "repair_prefix"]
    assert result["attempts"] == 3
    assert result["mentionsPath"] is True


def test_the_model_field_is_a_dropdown_of_what_the_provider_lists(tmp_path):
    """The design agent's model field offers exactly what the provider says it
    has (GET /api/models) -- so antigravity's gemini-4 and Claude models show
    up as agy adds them, with no list in this code to go stale. Switching
    provider preselects that provider's own default."""
    pytest.importorskip("quickjs")
    from webui.tests.jsdom_harness import exercise

    client = _client(tmp_path)
    sid = _new(client)["id"]
    view = client.get(f"/api/sessions/{sid}/bootstrap").json()
    listed = {"providers": [
        {"provider": "antigravity", "source": "agy models", "error": "", "models": [
            {"id": "gemini-4-argon-medium", "label": "gemini-4-argon-medium", "default": False},
            {"id": "claude-opus-5-5-high", "label": "claude-opus-5-5-high", "default": True}]},
        {"provider": "codex", "source": "", "error": "not signed in", "models": []}]}

    result = exercise(f"""
      var box = document.createElement('div');
      var panel = new __ns.BootstrapPanel(box, {{
        models: function () {{ return Promise.resolve({json.dumps(listed)}); }} }});
      panel.render({json.dumps(view)});
      panel.loadModels();
      return {{ panel: panel, box: box }};
    """, then="""
      var panel = __state.panel, box = __state.box;
      function walk(node, out) {
        (node && node._children || []).forEach(function (c) { out.push(c); walk(c, out); });
        return out;
      }
      function modelSelect() {
        return walk(box, []).filter(function (n) {
          return (n._children || []).some(function (c) {
            return c.textContent === '(provider default)'; });
        })[0];
      }
      function texts() {
        return walk(box, []).map(function (n) { return n.textContent || ''; }).join(' | ');
      }
      var draft = panel.draft.nodes.discussor;
      draft.provider = 'antigravity';
      draft.model = panel.defaultModel('antigravity');
      panel.draw();
      var select = modelSelect();
      var antigravity = {
        options: select._children.map(function (o) { return o.value; }),
        selected: select._children.filter(function (o) {
          // The attribute's presence, as in a browser: el() sets it to ''.
          return Object.prototype.hasOwnProperty.call(o, 'selected'); })
                                  .map(function (o) { return o.value; }),
        note: texts().indexOf('from agy models') !== -1,
      };
      draft.provider = 'codex';
      draft.model = panel.defaultModel('codex');
      panel.draw();
      return { antigravity: antigravity, codexSelect: !!modelSelect(),
               codexError: texts().indexOf('codex: not signed in') !== -1 };
    """)

    ag = result["antigravity"]
    assert ag["options"] == ["", "gemini-4-argon-medium", "claude-opus-5-5-high"], ag
    assert ag["selected"] == ["claude-opus-5-5-high"], "the provider's own default"
    assert ag["note"], "where the list came from"
    # A provider that cannot list falls back to typing the id, and says why.
    assert result["codexSelect"] is False and result["codexError"] is True, result
