"""
WHAT:  The acceptance test for the folder format.
WHY:   builtin_agents/default/ is today's four-role agent expressed as JSON.
       If these assertions -- the same ones the hand-written graph had to pass
       -- still hold when the graph is BUILT FROM THAT FOLDER, then the format
       is strong enough to express a real agent. If they ever fail, the format
       is too weak and needs extending, not the test relaxing.
CONCEPT: Testing a LangGraph app: build it with an InMemorySaver and a stub
       backend, then drive it with invoke() / Command(resume=...) exactly as
       the terminal does. No mocking of LangGraph itself.

Run with:   python tests/test_graph.py
"""

from __future__ import annotations

import os
import pathlib
import sys

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent.agentfolder.commands import build_registry
from agent.agentfolder.load import load_agent_folder
from agent.backends.base import Access, StructuredRun, Usage
from agent.work.compile import backends_needed, compile_agent
from agent.work.state import initial_work_state

DEFAULT = pathlib.Path(__file__).resolve().parents[1] / "agent" / "builtin_agents" / "default"


class RecordingBackend:
    """A stub that fills whatever model it is handed, and remembers the calls.

    It cannot hardcode field names, because the output models are BUILT AT
    RUNTIME from nodes.json -- which is itself a useful check that the
    generated models behave like hand-written ones.
    """

    name = "recording"
    supports_repo_access = True
    max_access = Access.FULL

    def __init__(self):
        self.calls: list[dict] = []
        self.turn = 0
        self.orchestrator_turns = 0

    def run_structured(self, *, thread_id, repo_path, access, developer_instructions,
                       prompt, output_model):
        self.turn += 1
        fields = {}

        for field_name, field in output_model.model_fields.items():
            if field_name == "action":
                # Drive one full loop: execute once, then finish.
                self.orchestrator_turns += 1
                fields[field_name] = "execute" if self.orchestrator_turns == 1 else "finish"
            elif field_name == "advice":
                fields[field_name] = "ready_to_plan"     # must NOT move the graph on
            elif field_name == "status":
                fields[field_name] = "done"
            elif field_name == "workstream":
                fields[field_name] = "main"
            elif field.annotation is str:
                fields[field_name] = f"{field_name}-{self.turn}"
            elif field.is_required():
                fields[field_name] = []

        data = output_model.model_validate(fields)
        self.calls.append({"model": output_model.__name__, "prompt": prompt,
                           "thread_id": thread_id, "access": access, "data": data,
                           "developer_instructions": developer_instructions})
        return StructuredRun(data=data, thread_id=thread_id or f"thread-{self.turn}",
                             is_new_thread=thread_id is None,
                             usage=Usage(input_tokens=100, cached_input_tokens=40))

    def close(self):
        pass

    def ran(self, model_name: str) -> bool:
        return any(c["model"] == model_name for c in self.calls)

    def prompts_for(self, model_name: str) -> list[str]:
        return [c["prompt"] for c in self.calls if c["model"] == model_name]


def new_session():
    folder = load_agent_folder(DEFAULT)
    backend = RecordingBackend()
    backends = {role: backend for role in backends_needed(folder)}
    graph = compile_agent(folder, backends=backends, checkpointer=InMemorySaver(),
                          registry=build_registry(folder))
    config = {"configurable": {"thread_id": "test"}, "recursion_limit": 100}
    state = initial_work_state(repo_path="/tmp", task_brief="add a login page",
                               agent_dir=str(DEFAULT))
    return backend, graph, config, graph.invoke(state, config=config)


# ---------------------------------------------------------------------------
# The five original assertions, now against the compiled folder
# ---------------------------------------------------------------------------

def test_discussor_never_reaches_planner_on_its_own():
    """Requirement 2, now enforced by graph.json rather than by graph.py."""
    backend, graph, config, _ = new_session()

    for answer in ["email login", "no oauth", "keep it simple"]:
        graph.invoke(Command(resume=answer), config=config)
        assert graph.get_state(config).next == ("human",), graph.get_state(config).next
        assert not backend.ran("PlannerOutput"), "the planner ran without /plan!"

    print("PASS  discussor never advanced on its own (3 turns, advice=ready_to_plan each)")


def test_transcript_accumulates_without_duplicating():
    backend, graph, config, _ = new_session()
    for answer in ["a", "b", "c"]:
        graph.invoke(Command(resume=answer), config=config)

    transcript = graph.get_state(config).values["transcript"]
    roles = [e["role"] for e in transcript]

    # 1 seed (the brief) + 3 human answers + 4 discussor replies
    assert len(transcript) == 8, f"expected 8, got {len(transcript)}: {roles}"
    assert roles == ["human", "discussor"] * 4, roles
    assert len({(e["role"], e["text"]) for e in transcript}) == 8, "entries duplicated"
    print("PASS  transcript: 8 entries, alternating, no duplication")


def test_plan_command_reaches_planner_with_full_conversation():
    backend, graph, config, _ = new_session()
    graph.invoke(Command(resume="email login"), config=config)
    graph.invoke(Command(resume="/plan focus on tests"), config=config)

    assert backend.ran("PlannerOutput"), "/plan did not reach the planner"
    prompt = backend.prompts_for("PlannerOutput")[0]
    for needle in ["add a login page", "email login", "reply-1", "focus on tests"]:
        assert needle in prompt, f"planner prompt missing {needle!r}\n---\n{prompt}"
    print("PASS  /plan reached the planner with both sides of the conversation")


def test_exit_ends_the_graph():
    backend, graph, config, _ = new_session()
    graph.invoke(Command(resume="/exit"), config=config)
    assert graph.get_state(config).next == (), "graph did not reach END"
    assert graph.get_state(config).values["outcome"] == "exit"
    print("PASS  /exit ends the run")


def test_full_cycle_reaches_the_executor():
    backend, graph, config, _ = new_session()
    graph.invoke(Command(resume="/plan"), config=config)
    assert backend.ran("ExecutorOutput"), "never reached the executor"
    print("PASS  /plan -> planner -> orchestrator -> executor -> finish")


# ---------------------------------------------------------------------------
# Two more the folder format specifically needs
# ---------------------------------------------------------------------------

def test_threads_accumulate_across_nodes():
    """The operator.or_ lesson: one node writing `threads` must not erase others.

    With merge_section() this would be a bug, because the code does not know
    the keys -- they come from nodes.json. See agent/work/state.py.
    """
    backend, graph, config, _ = new_session()
    graph.invoke(Command(resume="/plan"), config=config)

    threads = graph.get_state(config).values["threads"]
    assert set(threads) >= {"discussor/", "planner/", "orchestrator/", "executor/main"}, threads
    # executor/main, not executor/ -- thread_key rendered the workstream.
    print(f"PASS  threads accumulated without erasure: {sorted(threads)}")


def test_executor_gets_the_plan_first_then_the_short_prompt():
    """refresh_on + bump + thread_marks reproduce executor_seen_plan_generation."""
    backend, graph, config, _ = new_session()
    graph.invoke(Command(resume="/plan"), config=config)

    executor_prompts = backend.prompts_for("ExecutorOutput")
    assert executor_prompts, "executor never ran"
    first = executor_prompts[0]
    assert "Overall plan:" in first, "the executor's first prompt should carry the whole plan"

    marks = graph.get_state(config).values["thread_marks"]
    counters = graph.get_state(config).values["counters"]
    assert counters.get("plan_revision") == 1, counters
    assert marks.get("executor/main") == 1, marks
    print("PASS  executor got the full plan first; thread_marks tracks the plan revision")


def test_write_nodes_are_told_where_output_goes():
    """A run's output must be scoped to the run, not scattered in the repo.

    A real run invented top-level artifacts/, configs/ and docs/ directories
    plus a model cache in someone's project, because nothing told it where
    generated files belong. The rules are appended by the LOADER rather than
    written into the folder, so a generated agent cannot opt out of them.
    """
    folder = load_agent_folder(DEFAULT)
    backend = RecordingBackend()
    artifacts = "/somewhere/.agent/sessions/task-x/artifacts"

    graph = compile_agent(
        folder,
        backends={role: backend for role in backends_needed(folder)},
        checkpointer=InMemorySaver(),
        registry=build_registry(folder),
        artifacts_dir=artifacts,
    )
    config = {"configurable": {"thread_id": "artifacts"}, "recursion_limit": 100}
    graph.invoke(initial_work_state(repo_path="/tmp", task_brief="do it",
                                    agent_dir=str(DEFAULT), artifacts_dir=artifacts),
                 config=config)
    graph.invoke(Command(resume="/plan"), config=config)

    by_access = {}
    for call in backend.calls:
        by_access.setdefault(call["access"].value, []).append(call["developer_instructions"])

    for instructions in by_access.get("write", []):
        assert artifacts in instructions, "a write node was not told where output goes"
        assert ".agent/" in instructions, "a write node lost the infrastructure warning"
    assert by_access.get("write"), "no write node ran, so this test proved nothing"

    # Read-only nodes have nothing to write, so they are not burdened with it.
    for instructions in by_access.get("read_only", []):
        assert artifacts not in instructions, "a read-only node got write rules"

    print("PASS  write nodes are told to put output in the run's artifacts dir")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nAll acceptance tests passed -- the folder format expresses today's agent.")
