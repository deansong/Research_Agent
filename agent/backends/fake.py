"""
WHAT:  A backend that returns canned answers instead of calling a model.
WHY:   So you can drive the whole graph -- commands, routing, interrupts,
       resume, the transcript reducer -- for free and instantly, while you
       are learning or while you are changing the graph's shape.
CONCEPT: A test double for the AgentBackend protocol.  Note that it does not
       import base.py at all: satisfying a Protocol means having the right
       attributes, not inheriting from anything.

Use it with:   python main.py run /path/to/repo --backend fake
"""

from __future__ import annotations

import itertools

from research_backend.base import Access, StructuredRun, Usage, OutputT
from research_backend.backends.fake import (  # noqa: F401 -- shared helpers
    _choices, _fake_events, _placeholder_fields)


class FakeBackend:
    """Fills every field of the requested schema with obvious placeholder text."""

    name = "fake"
    supports_repo_access = False
    # Claims FULL so it can stand in for any role, including the executor.
    # It never actually touches a file -- it never runs anything.
    max_access = Access.FULL

    def __init__(self, *, model: str | None = None, **options):
        self.model = model
        self.counter = itertools.count(1)
        self.orchestrator_turns = 0

    def run_structured(
        self,
        *,
        thread_id: str | None,
        repo_path: str,
        access: Access,
        developer_instructions: str,
        prompt: str,
        output_model: type[OutputT],
    ) -> StructuredRun[OutputT]:
        turn = next(self.counter)
        print(f"[fake] pretending to run {output_model.__name__} (turn {turn})")

        # A designer is asked for a whole agent folder, which is far too
        # structured to fill in generically. Returning a real, minimal, VALID
        # agent instead is what makes `--backend fake` able to demonstrate the
        # entire two-phase flow -- design, write, validate, then run the thing
        # that was designed -- with no API key and no network.
        if _looks_like_a_planner(output_model):
            return self._canned(output_model, _MINIMAL_PLAN, turn,
                                "a small two-step plan")
        if _looks_like_a_designer(output_model):
            # What a real designer is shown is a valid starting design built
            # from the plan; accepting it unchanged is a legitimate answer,
            # and it is the only canned one that honours the rule that the
            # work nodes are exactly the plan's steps.
            shown = _shown_design(prompt)
            if shown is not None:
                shown["graph"]["name"] = "fake-generated"
                return self._canned(output_model, {**shown, "task_brief": _MINIMAL_AGENT[
                    "task_brief"], "rationale": "[fake] accepted the starting design"},
                    turn, "the starting design it was shown, unchanged")
            return self._canned(output_model, _MINIMAL_AGENT, turn,
                                "a minimal but genuinely valid two-node agent")

        if _looks_like_an_orchestrator(output_model):
            print("[fake] returning a decision read off the [state] line")
            # It CONTINUES the conversation it was given, like a real one:
            # that is what carries the discussor into the run.
            return StructuredRun(
                data=output_model.model_validate(_orchestrate(output_model, prompt)),
                thread_id=thread_id or f"fake-thread-{turn}",
                is_new_thread=thread_id is None,
                usage=Usage(input_tokens=1000, cached_input_tokens=400, output_tokens=50,
                            context_window=200_000),
                events=_fake_events(turn))

        fields = _placeholder_fields(output_model, turn)

        # Special case so a fake run actually TERMINATES. A node with an
        # "action" enum is a router; left to the first choice it would pick
        # "execute" forever and bounce until the recursion limit. Detected by
        # FIELD NAME, not class name -- output models are built at runtime from
        # nodes.json and are named after whatever the folder called the node.
        if "action" in fields and "finish" in _choices(output_model, "action"):
            self.orchestrator_turns += 1
            fields["action"] = "execute" if self.orchestrator_turns == 1 else "finish"
            if "final_summary" in fields:
                fields["final_summary"] = "[fake] pretend work finished"

        data = output_model.model_validate(fields)

        return StructuredRun(
            data=data,
            thread_id=thread_id or f"fake-thread-{turn}",
            is_new_thread=thread_id is None,
            usage=Usage(input_tokens=100 * turn, cached_input_tokens=40 * turn,
                        output_tokens=10, context_window=200_000),
            events=_fake_events(turn),
        )

    def _canned(self, output_model, payload: dict, turn: int, what: str):
        """Return a hand-written valid object for a model too structured to fill in.

        PlannerOutput and DesignerOutput both nest several models deep, so the
        generic field-filler cannot produce something that validates. Canning a
        REAL one is what lets --backend fake demonstrate the whole pipeline --
        discuss, plan, approve, design, write, validate, run -- offline.
        """
        print(f"[fake] returning {what}")
        return StructuredRun(
            data=output_model.model_validate(payload),
            thread_id=f"fake-{turn}",
            is_new_thread=True,
            usage=Usage(input_tokens=500, cached_input_tokens=100, output_tokens=200),
            events=_fake_events(turn),
        )

    def close(self) -> None:
        return None


def _looks_like_an_orchestrator(output_model) -> bool:
    """A hub agent's orchestrator -- checked BEFORE the older `action`
    router special case below, which would otherwise catch it."""
    return {"action", "target", "subject", "include"} <= set(output_model.model_fields)


def _orchestrate(output_model, prompt: str) -> dict:
    """A plausible orchestrator, played from the [state] line its prompt ends
    with: do each step in order, validate each, send a failure to where the
    validator says it started, then the paper, then finish."""
    import re

    line = next((l for l in reversed(prompt.splitlines()) if l.startswith("[state]")), "")
    state = dict(re.findall(r"(\w+)=(\S+)", line))
    targets = [t for t in _choices(output_model, "target") if t != "none"]
    kind, todo = state.get("kind", "start"), state.get("todo", "none")
    decision = {"action": "finish", "target": "none", "subject": "none",
                "reason": f"[fake] after {kind}", "handoff": "[fake] do your step",
                "include": [], "thread": "fresh", "step_status": [], "note": "",
                "verdict": "done", "summary": "[fake] every step done and checked",
                "findings": [], "plan_feedback": "", "design_feedback": ""}
    if kind in ("work", "paper") and state.get("last") in targets:
        decision.update(action="validate", subject=state["last"], thread="fresh")
    elif kind == "validation" and state.get("verdict") not in ("pass", "none"):
        origin = state.get("origin")
        decision.update(action="dispatch",
                        target=origin if origin in targets else state.get("subject", todo),
                        handoff="[fake] fix what the validator found")
    elif todo in targets:
        decision.update(action="dispatch", target=todo)
    return decision


def _looks_like_a_planner(output_model) -> bool:
    return {"summary", "steps"} <= set(output_model.model_fields)


# A plan the canned agent below actually maps onto: worker owns step 1,
# review owns step 2. Small on purpose -- it exists to prove the pipeline.
_MINIMAL_PLAN = {
    "summary": "[fake] Look at the repository, then report what is there.",
    "steps": [
        {"id": "1", "title": "Inspect the repository",
         "detail": "Read what is there and note the conventions.",
         "substeps": [
             # A check on a SUBSTEP, so the offline path carries one through
             # steps_for() into a node's prompt. Without it --backend fake
             # rehearses the design phase without ever exercising the field a
             # verifier node is built from.
             {"id": "1.1", "title": "List the files", "detail": "",
              "check": "the file list names the entry point"},
             {"id": "1.2", "title": "Read the entry points", "detail": ""},
         ]},
        {"id": "2", "title": "Report back to the human",
         "detail": "Summarise findings and stop for review.", "substeps": []},
    ],
}


def _shown_design(prompt: str) -> dict | None:
    """The design at the end of a designer prompt, or None if there is none."""
    import json

    marker = '\n\n{\n "graph"'
    if marker not in prompt:
        return None
    try:
        return json.loads(prompt[prompt.rindex(marker) + 2:])
    except json.JSONDecodeError:
        return None


def _looks_like_a_designer(output_model) -> bool:
    """Detected by field names, not class name -- the class is built at runtime."""
    return {"task_brief", "graph", "nodes"} <= set(output_model.model_fields)


# A complete agent: do the work in one step, then show the human and let them
# run it again or stop. Small on purpose -- it is here to prove the pipeline,
# not to be impressive.
_MINIMAL_AGENT = {
    "task_brief": "[fake] Do the task described when this session started.",
    "rationale": "[fake] A single worker plus a review stop is the smallest useful shape.",
    "graph": {
        "name": "fake-generated",
        "description": "A minimal agent produced by the fake backend.",
        "entry": "worker",
        "nodes": [
            {"name": "worker", "kind": "agent"},
            {"name": "review", "kind": "human"},
        ],
        "edges": [
            {"from": "worker", "to": "review",
             "ask": {"purpose": "review", "resume_to": "worker",
                     "question": "The worker reported: {out.worker.summary}. What next?",
                     "context": "{out.worker.files}"}},
        ],
        "branches": [],
    },
    # In the designer's answer format: a short spec per node, from which
    # bootstrap/spec.py writes the prompts and output fields.
    "nodes": [
        {
            "name": "worker",
            "role": "worker",
            # Every step the plan has, not just the first. A design that
            # owns only step 1 leaves the rest assigned to nobody, which is a
            # validation error -- and the fake agent should be a valid
            # design, since it is what --backend fake exercises everything
            # with. 1 and 2 cover the fake planner's own two steps.
            "steps": ["1", "2"],
            "instructions": "Do exactly what the brief asks and report back.",
        },
        {
            "name": "review",
            "role": "human",
            "commands": [
                {"name": "again", "to": "worker", "summary": "Have another go"},
                {"name": "exit", "to": "__end__", "aliases": ["quit", "q"],
                 "summary": "Finish"},
            ],
        },
    ],
}
