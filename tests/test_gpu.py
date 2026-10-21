"""
WHAT:  A Codex role can be given the GPUs: its write turns then run without
       Codex's sandbox. Every turn still inside the sandbox is told it hides
       the GPUs.
WHY:   Measured: Codex's workspace-write sandbox has no /dev/nvidia*, so a
       preflight in it reported "no GPU" as a blocker on a machine with seven.

Run with:   python -m pytest tests/test_gpu.py -q
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from openai_codex import Sandbox  # noqa: E402

from agent.backends.base import Access  # noqa: E402
from agent.backends.codex import CodexBackend  # noqa: E402
from agent.work.compile import SANDBOX_GPU_NOTE, sandbox_note  # noqa: E402


@pytest.mark.parametrize("gpu, access, sandbox", [
    (False, Access.WRITE, Sandbox.workspace_write),
    (True, Access.WRITE, Sandbox.full_access),         # the GPUs, no sandbox
    (True, Access.READ_ONLY, Sandbox.read_only),       # a reader keeps its sandbox
])
def test_gpu_switches_off_the_sandbox_for_write_turns_only(gpu, access, sandbox):
    client = MagicMock()
    CodexBackend(client, gpu=gpu)._get_thread(
        thread_id=None, repo_path="/w", access=access, developer_instructions="")
    assert client.thread_start.call_args.kwargs["sandbox"] == sandbox


def test_the_note_goes_to_every_turn_still_in_the_sandbox():
    codex, gpu = SimpleNamespace(name="codex", gpu=False), SimpleNamespace(name="codex", gpu=True)
    assert sandbox_note(codex, "write") == SANDBOX_GPU_NOTE
    assert sandbox_note(gpu, "write") == "", "it can see them"
    assert sandbox_note(gpu, "read_only") == SANDBOX_GPU_NOTE, "a checker is still sandboxed"
    assert sandbox_note(SimpleNamespace(name="antigravity"), "write") == ""
    assert "never conclude" in SANDBOX_GPU_NOTE.lower()


def test_a_session_gives_a_codex_role_the_gpus_from_runs_on():
    from tests.test_supervisor import _answer, _client, _until
    from webui.tests.conftest import temp_repo

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
        role = next(r for r in client.get(f"/api/sessions/{sid}/agent").json()["backends"]
                    if r != "checker")

        on = client.put(f"/api/sessions/{sid}/roles/{role}",
                        json={"provider": "codex", "model": "gpt-5.6-sol", "gpu": True})
        assert on.status_code == 200, on.text
        assert on.json()["backends"][role]["gpu"] is True and "GPUs" in on.json()["detail"]
        saved = json.loads((session / "options.json").read_text())
        assert saved["role_options"] == {role: {"gpu": True}}

        # Moving off Codex drops it: nothing else has a sandbox to switch off.
        off = client.put(f"/api/sessions/{sid}/roles/{role}", json={"provider": "fake"})
        assert off.json()["backends"][role]["gpu"] is False
        assert "role_options" not in json.loads((session / "options.json").read_text())


# ---- moving a node to another provider mid-run ---------------------------------

def _node(backend, session_dir=""):
    from pydantic import BaseModel

    from agent.agentfolder.schema import AgentNodeConfig, EdgeSpec
    from agent.work.templates.agent_node import make_agent_node

    class Out(BaseModel):
        summary: str = ""

    config = AgentNodeConfig.model_validate({
        "backend": "coder", "access": "write", "instructions": "i",
        "prompts": {"first": "FIRST", "next": "NEXT"},
        "output": [{"name": "summary", "type": "string", "required": True}]})
    return make_agent_node("s4", config, backend=backend, output_model=Out,
                           transition=EdgeSpec.model_validate({"from": "s4", "to": "__end__"}),
                           session_dir=session_dir)


class _Recording:
    def __init__(self, name):
        self.name, self.model, self.calls = name, "m", []

    def run_structured(self, **kw):
        from agent.backends.base import StructuredRun
        self.calls.append(kw)
        return StructuredRun(data=kw["output_model"](summary="ok"), thread_id=f"{self.name}-new",
                             is_new_thread=True, usage=None, events=[])


def _state(**extra):
    return {"repo_path": "/w", "task_brief": "t", "plan": {}, "outputs": {}, "counters": {},
            "vars": {}, "route": {}, "usage": {}, "thread_marks": {}, "artifacts_dir": "",
            "threads": {"s4/": "codex-thread-1"}, **extra}


def test_a_node_moved_to_another_provider_starts_a_new_conversation():
    agy = _Recording("antigravity")
    update = _node(agy)(_state(thread_providers={"s4/": "codex"}))
    call = agy.calls[0]
    assert call["thread_id"] is None, "a Codex thread id means nothing to agy"
    assert call["prompt"] == "FIRST", "and the new model gets the whole first prompt"
    assert update["thread_providers"] == {"s4/": "antigravity"}


def test_staying_on_the_same_provider_continues_the_conversation():
    codex = _Recording("codex")
    _node(codex)(_state(thread_providers={"s4/": "codex"}))
    assert codex.calls[0]["thread_id"] == "codex-thread-1" and codex.calls[0]["prompt"] == "NEXT"


def test_a_run_from_before_providers_were_recorded_asks_the_activity_record(tmp_path):
    from agent import activity

    activity.write(tmp_path, "s4", [], sent={"prompt": "p"},
                   backend=SimpleNamespace(name="codex", model="m"), thread_id="codex-thread-1")
    agy = _Recording("antigravity")
    _node(agy, str(tmp_path))(_state())           # no thread_providers at all
    assert agy.calls[0]["thread_id"] is None and agy.calls[0]["prompt"] == "FIRST"
