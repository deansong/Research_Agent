"""
WHAT:  The research state: changes by id, versions, approvals that lapse when
       their section changes, history on disk, and the Markdown views nodes
       are given.
WHY:   Every node's context is built from this state, and nodes only ever
       propose changes to it -- so applying them has to be exact, all or
       nothing, and traceable.

Run with:   python -m pytest tests/test_state.py -q
"""

from __future__ import annotations

import json

import pytest

from research_agent import state as S


def _with_hypotheses():
    st, changed = S.apply(S.new(), [
        {"op": "add", "section": "hypotheses",
         "value": {"statement": "Rank matters", "prediction": "r=64 beats r=4 by 2 F1"}},
        {"op": "add", "section": "hypotheses", "value": {"statement": "It does not"}},
        {"op": "merge", "section": "goal", "value": {"question": "Does LoRA rank matter?"}},
    ], by="planner")
    return st, changed


def test_items_get_ids_and_sections_get_versions():
    st, changed = _with_hypotheses()
    assert [h["id"] for h in st["hypotheses"]] == ["H1", "H2"]
    assert changed == ["hypotheses", "goal"]
    assert S.version(st, "hypotheses") == 1 and S.version(st, "goal") == 1
    st, _ = S.apply(st, [{"op": "update", "section": "hypotheses", "id": "H2",
                          "value": {"status": "selected"}}], by="user")
    assert st["hypotheses"][1]["status"] == "selected" and S.version(st, "hypotheses") == 2
    st, _ = S.apply(st, [{"op": "add", "section": "hypotheses", "value": {"statement": "x"}}],
                    by="planner")
    assert st["hypotheses"][-1]["id"] == "H3", "ids keep counting"


def test_dropping_keeps_what_must_be_kept():
    st, _ = _with_hypotheses()
    st, _ = S.apply(st, [{"op": "drop", "section": "hypotheses", "id": "H1"}], by="user")
    assert st["hypotheses"][0]["status"] == "dropped", "a hypothesis is marked, not deleted"
    st, _ = S.apply(st, [{"op": "add", "section": "decisions", "value": {"what": "use SST-2"}},
                         {"op": "add", "section": "risks", "value": {"text": "small data"}}],
                    by="discussor")
    assert st["decisions"][0]["by"] == "discussor" and st["decisions"][0]["at"]
    with pytest.raises(S.StateError, match="decisions are kept"):
        S.apply(st, [{"op": "drop", "section": "decisions", "id": "D1"}], by="x")
    st, _ = S.apply(st, [{"op": "drop", "section": "risks", "id": "K1"}], by="x")
    assert st["risks"] == []


def test_a_bad_op_changes_nothing():
    st, _ = _with_hypotheses()
    for bad, message in (
            ({"op": "update", "section": "hypotheses", "id": "H9", "value": {}}, "No hypotheses item 'H9'"),
            ({"op": "add", "section": "nowhere", "value": {}}, "no section"),
            ({"op": "update", "section": "hypotheses", "id": "H1",
              "value": {"status": "maybe"}}, "status"),
            ({"op": "set", "section": "hypotheses", "value": {}}, "use add, update or drop")):
        with pytest.raises((S.StateError, ValueError), match=message):
            S.apply(st, [{"op": "add", "section": "risks", "value": {"text": "r"}}, bad], by="x")
    assert st.get("risks") == [] and S.version(st, "risks") == 0


def test_an_approval_lapses_when_its_section_changes():
    st, _ = _with_hypotheses()
    assert not S.approved(st, "hypotheses")
    with pytest.raises(S.StateError, match="no plan to approve"):
        S.apply(st, [{"op": "approve", "section": "plan"}], by="user")
    st, _ = S.apply(st, [{"op": "approve", "section": "hypotheses"}], by="user")
    assert S.approved(st, "hypotheses")
    st, _ = S.apply(st, [{"op": "update", "section": "hypotheses", "id": "H1",
                          "value": {"prediction": "changed"}}], by="planner")
    assert not S.approved(st, "hypotheses")
    assert "## Hypotheses (v2, not approved)" in S.render(st, ["hypotheses"])


def test_what_changed_since_someone_last_looked():
    st, _ = _with_hypotheses()
    seen = dict(st["versions"])
    st, _ = S.apply(st, [{"op": "set", "section": "plan", "value": {
        "summary": "two runs", "steps": [{"id": "1", "title": "Train", "rubric": ["R1"]}]}}],
        by="planner")
    assert S.changed_since(st, seen) == ["plan"]
    assert S.changed_since(st, {}) == ["goal", "hypotheses", "plan"]


def test_rendering_is_compact_when_asked():
    st, _ = _with_hypotheses()
    st, _ = S.apply(st, [{"op": "add", "section": "evidence", "value": {
        "claim": "LoRA matches full fine-tuning", "source": "arXiv:2106.09685",
        "verified": True, "bears_on": ["H1"]}}], by="researcher")
    full = S.render(st, ["hypotheses", "evidence"])
    assert "prediction: r=64 beats r=4 by 2 F1" in full
    assert "- E1 [verified] LoRA matches full fine-tuning -- arXiv:2106.09685 (bears on H1)" in full
    brief = S.render(st, ["hypotheses"], brief=True)
    assert "prediction" not in brief and "- H1 [proposed] Rank matters" in brief
    assert S.render(st, ["risks"]) == "", "empty sections are left out"
    assert st["evidence"][0]["found_by"] == "researcher"


def test_the_store_keeps_the_state_its_rendering_and_every_change(tmp_path):
    store = S.Store(tmp_path)
    assert store.read() == S.new()
    ops = [{"op": "merge", "section": "goal", "value": {"question": "Q?"}}]
    st, changed = S.apply(store.read(), ops, by="discussor")
    store.write(st, by="discussor", ops=ops, changed=changed)
    assert store.read()["goal"]["question"] == "Q?"
    assert "Question: Q?" in (tmp_path / "state" / "research.md").read_text()
    record = json.loads((tmp_path / "state" / "history" / "0001.json").read_text())
    assert record["by"] == "discussor" and record["ops"] == ops and record["changed"] == ["goal"]


def test_questions_keep_their_for_field():
    st, _ = S.apply(S.new(), [{"op": "add", "section": "open_questions",
                               "value": {"text": "GPU budget?", "for": "user"}}], by="o")
    assert st["open_questions"][0]["for"] == "user"
    assert "(for user, open) GPU budget?" in S.render(st, ["open_questions"])
