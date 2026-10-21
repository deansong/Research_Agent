"""
WHAT:  Delegation, only when chosen: a role whose provider is out of credits
       runs its turns on the delegate you picked -- a fresh conversation with
       the first prompt -- instead of waiting.
WHY:   A delegate is a different model, so it must never happen by itself:
       unticked, a role waits for credits exactly as before.

Run with:   python -m pytest tests/test_delegate.py -q
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from types import SimpleNamespace

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from agent.backends.base import BackendOutOfCredits  # noqa: E402
from agent.backends.delegate import Delegated, DelegatingBackend  # noqa: E402
from tests.test_gpu import _node, _Recording, _state  # noqa: E402


class _Broke(_Recording):
    credit_wait_hours = 24.0

    def run_structured(self, **kw):
        self.calls.append(kw)
        raise BackendOutOfCredits("You've hit your usage limit.")


def test_out_of_credits_switches_to_the_delegate_for_the_rest_of_the_run():
    codex, agy = _Broke("codex"), _Recording("antigravity")
    backend = DelegatingBackend(codex, agy, "coder")
    assert codex.credit_wait_hours == 0, "a delegating role does not wait"
    backend.cancel = "stop-event"
    assert agy.cancel == "stop-event" == codex.cancel, "Stop reaches both"

    from pydantic import BaseModel

    class Out(BaseModel):
        summary: str = ""

    with pytest.raises(Delegated):
        backend.run_structured(prompt="p", output_model=Out)
    assert backend.name == "antigravity" and backend.delegated
    backend.run_structured(prompt="q", output_model=Out)
    assert len(codex.calls) == 1 and agy.calls[0]["prompt"] == "q", "stays delegated"


def test_the_node_redoes_the_turn_on_the_delegate_fresh():
    codex, agy = _Broke("codex"), _Recording("antigravity")
    update = _node(DelegatingBackend(codex, agy, "coder"))(
        _state(thread_providers={"s4/": "codex"}))
    assert codex.calls[0]["thread_id"] == "codex-thread-1" and codex.calls[0]["prompt"] == "NEXT"
    assert agy.calls[0]["thread_id"] is None, "a Codex thread means nothing to agy"
    assert agy.calls[0]["prompt"] == "FIRST", "the delegate has seen none of it"
    assert update["thread_providers"] == {"s4/": "antigravity"}
    assert update["threads"] == {"s4/": "antigravity-new"}


def test_a_session_chooses_a_delegate_per_role_and_a_run_uses_it(monkeypatch):
    from agent.backends.fake import FakeBackend
    from tests.test_supervisor import _answer, _client, _until
    from webui.tests.conftest import temp_repo

    original = FakeBackend.run_structured

    def broke_when_told(self, **kw):
        if self.model == "broke":
            raise BackendOutOfCredits("out of credits")
        return original(self, **kw)

    monkeypatch.setattr(FakeBackend, "run_structured", broke_when_told)
    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo, "off")
        sid = client.post("/api/sessions", json={
            "repo": str(repo), "task": "t", "backend": "fake", "research": False}).json()["id"]
        session = repo / ".agent" / "sessions" / sid
        _until(client, sid, purpose="discussion")
        _answer(client, sid, "/plan")
        _until(client, sid, purpose="plan_review")
        _answer(client, sid, "/approve")
        _until(client, sid, purpose="design_review")
        backends = client.get(f"/api/sessions/{sid}/agent").json()["backends"]
        writer = next(r for r in backends if r != "checker")
        reader = "checker"

        # Untouched: no delegate.
        assert backends[writer]["delegate"] is None

        # Antigravity cannot stand in for a read-only checker: refused.
        refused = client.put(f"/api/sessions/{sid}/roles/{reader}", json={
            "provider": "fake", "delegate": {"provider": "antigravity", "model": ""}})
        assert refused.status_code == 422 and "read_only" in refused.text

        # The writer's provider is "out of credits"; its delegate is a stand-in.
        chosen = client.put(f"/api/sessions/{sid}/roles/{writer}", json={
            "provider": "fake", "model": "broke",
            "delegate": {"provider": "fake", "model": "standin"}})
        assert chosen.status_code == 200, chosen.text
        assert chosen.json()["backends"][writer]["delegate"] == {"provider": "fake",
                                                                 "model": "standin"}
        assert f"{writer}=fake:standin" in json.loads(
            (session / "options.json").read_text())["delegate"]

        _answer(client, sid, "/approve")
        _until(client, sid, purpose="findings")
        node = backends[writer]["nodes"][0]
        from agent import activity
        turn = activity.latest(session, node)
        assert turn.model == "standin", "the turn ran on the delegate"
        # The record keeps it (as text: sent values are clipped to strings).
        assert str(turn.sent.get("delegated")) == "True" and turn.sent["template"] == "first"

        # Unticked: delegation goes, the role waits again.
        off = client.put(f"/api/sessions/{sid}/roles/{writer}", json={"provider": "fake"})
        assert off.json()["backends"][writer]["delegate"] is None
