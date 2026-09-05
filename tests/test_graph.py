"""
WHAT:  End-to-end tests for the graph, using the `fake` backend.
WHY:   Also a worked example of how you test a LangGraph app at all: build the
       graph with an InMemorySaver and a stub backend, then drive it with
       invoke() / Command(resume=...) exactly as the terminal does.
CONCEPT: Testing interrupt/resume. Note there is no mocking of LangGraph
       itself -- the real graph runs, the real checkpointer records it.

Run with:   python tests/test_graph.py
       or:  pytest tests/
"""

from __future__ import annotations

import os
import pathlib
import sys

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent import roles
from agent.backends.base import Access, StructuredRun, Usage
from agent.graph import build_graph
from agent.schemas import DiscussorOutput, ExecutorOutput, OrchestratorOutput, PlannerOutput
from agent.state import create_initial_state


class RecordingBackend:
    """A fake backend that also records every call, so tests can assert on
    WHICH role ran and WHAT prompt it was given."""

    name = "recording"
    supports_repo_access = True
    max_access = Access.FULL

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.turn = 0
        self.orchestrator_turns = 0

    def run_structured(self, *, thread_id, repo_path, access, developer_instructions,
                       prompt, output_model):
        self.turn += 1
        self.calls.append((output_model.__name__, prompt))

        if output_model is DiscussorOutput:
            # Always claims to be ready -- the point is that this must NOT
            # move the graph on by itself.
            data = DiscussorOutput(reply=f"reply-{self.turn}", question=f"q-{self.turn}?",
                                   requirements=f"brief-{self.turn}", advice="ready_to_plan")
        elif output_model is PlannerOutput:
            data = PlannerOutput(plan=f"plan-{self.turn}", workstream="main",
                                 next_task=f"task-{self.turn}")
        elif output_model is OrchestratorOutput:
            self.orchestrator_turns += 1
            action = "execute" if self.orchestrator_turns == 1 else "finish"
            data = OrchestratorOutput(action=action, final_summary="done")
        else:
            data = ExecutorOutput(status="done", summary="implemented")

        return StructuredRun(data=data, thread_id=f"thread-{output_model.__name__}",
                             is_new_thread=thread_id is None,
                             usage=Usage(input_tokens=100, cached_input_tokens=40))

    def close(self):
        pass

    def ran(self, model_name: str) -> bool:
        return any(c[0] == model_name for c in self.calls)

    def prompt_for(self, model_name: str) -> str:
        return next(c[1] for c in self.calls if c[0] == model_name)


def new_session():
    backend = RecordingBackend()
    graph = build_graph({r: backend for r in roles.ALL_ROLES}, InMemorySaver())
    config = {"configurable": {"thread_id": "test"}, "recursion_limit": 100}
    state = create_initial_state(pathlib.Path("/tmp"), "add a login page")
    return backend, graph, config, graph.invoke(state, config=config)


def test_discussor_never_reaches_planner_on_its_own():
    """Requirement 2: only /plan moves the graph to the planner."""
    backend, graph, config, _ = new_session()

    for answer in ["email login", "no oauth", "keep it simple"]:
        graph.invoke(Command(resume=answer), config=config)
        assert graph.get_state(config).next == ("human",), "graph left the human"
        assert not backend.ran("PlannerOutput"), "the planner ran without /plan!"

    print("PASS  discussor never advanced on its own (3 turns, advice=ready_to_plan each)")


def test_transcript_accumulates_without_duplicating():
    """The operator.add reducer must append deltas, not re-add whole lists."""
    backend, graph, config, _ = new_session()
    for answer in ["a", "b", "c"]:
        graph.invoke(Command(resume=answer), config=config)

    transcript = graph.get_state(config).values["transcript"]
    roles_seen = [e["role"] for e in transcript]

    # 1 seed (the original request) + 3 human answers + 4 discussor replies
    assert len(transcript) == 8, f"expected 8 entries, got {len(transcript)}"
    assert roles_seen == ["human", "discussor"] * 4, roles_seen
    assert len({(e["role"], e["text"]) for e in transcript}) == 8, "entries duplicated"
    print("PASS  transcript: 8 entries, alternating, no duplication")


def test_plan_command_reaches_planner_with_full_conversation():
    backend, graph, config, _ = new_session()
    graph.invoke(Command(resume="email login"), config=config)
    graph.invoke(Command(resume="/plan focus on tests"), config=config)

    assert backend.ran("PlannerOutput"), "/plan did not reach the planner"
    prompt = backend.prompt_for("PlannerOutput")
    for needle in ["add a login page", "email login", "reply-1", "q-1?", "focus on tests"]:
        assert needle in prompt, f"planner prompt missing {needle!r}"
    print("PASS  /plan reached the planner with both sides of the conversation")


def test_exit_ends_the_graph():
    backend, graph, config, _ = new_session()
    graph.invoke(Command(resume="/exit"), config=config)
    assert graph.get_state(config).next == (), "graph did not reach END"
    print("PASS  /exit ends the run")


def test_full_cycle_reaches_the_executor():
    backend, graph, config, _ = new_session()
    graph.invoke(Command(resume="/plan"), config=config)
    assert backend.ran("ExecutorOutput"), "never reached the executor"
    assert graph.get_state(config).values["final_summary"] == "done"
    print("PASS  /plan -> planner -> orchestrator -> executor -> finish")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nAll tests passed.")
