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

from agent.backends.base import Access, StructuredRun, Usage, OutputT


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
            return self._canned(output_model, _MINIMAL_AGENT, turn,
                                "a minimal but genuinely valid two-node agent")

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
        )

    def close(self) -> None:
        return None


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
             {"id": "1.1", "title": "List the files", "detail": ""},
             {"id": "1.2", "title": "Read the entry points", "detail": ""},
         ]},
        {"id": "2", "title": "Report back to the human",
         "detail": "Summarise findings and stop for review.", "substeps": []},
    ],
}


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
                     "context": "{out.worker.detail}"}},
        ],
        "branches": [],
    },
    "nodes": [
        {
            "name": "worker",
            "backend": "worker",
            "access": "read_only",
            "steps": ["1"],
            "instructions": "You are a worker. Do exactly what the brief asks and report back.",
            "output": [
                {"name": "summary", "type": "string", "required": True},
                {"name": "detail", "type": "string"},
            ],
            "prompts": {
                # Shows the context-control shape a real designer should copy:
                # this node's OWN steps in full, everyone else's as one line.
                "first": "Your task:\n\n{task_brief}\n\n"
                         "The steps you are responsible for:\n{my_steps}\n\n"
                         "The wider plan, for context only:\n{plan_outline}\n\n"
                         "Do your steps and report what you found.",
                "next": "The human said:\n\n{last_answer}\n\nContinue.",
            },
            "announce": "working...",
            "record": "{out.worker.summary}",
        },
        {
            "name": "review",
            "commands": [
                {"name": "again", "to": "worker", "summary": "Have another go"},
                {"name": "exit", "to": "__end__", "aliases": ["quit", "q"],
                 "summary": "Finish"},
            ],
        },
    ],
}


def _placeholder_fields(output_model, turn: int) -> dict:
    """Build a dict that satisfies `output_model`, whatever its fields are.

    EVERY field is filled, not just the required ones. That matters: a node's
    prompts read other nodes' outputs via {out.x.y}, and its thread_key may be
    "{out.planner.workstream}". If optional fields came back empty, prompts
    would render blank and every thread key would collapse to the same value --
    so a fake run would not exercise the parts most likely to be wrong.
    """
    import typing

    values: dict[str, object] = {}
    for name, field in output_model.model_fields.items():
        annotation = field.annotation
        origin = typing.get_origin(annotation)

        if origin is typing.Literal:
            values[name] = typing.get_args(annotation)[0]
        elif origin in (list, set, tuple):
            values[name] = [f"[fake {name} 1]", f"[fake {name} 2]"]
        elif annotation is bool:
            values[name] = False
        elif annotation is int:
            values[name] = turn
        elif name == "workstream":
            # A stable value, so per-workstream thread keys actually group.
            values[name] = "main"
        else:
            values[name] = f"[fake {name} #{turn}]"

    return values


def _choices(output_model, field_name: str) -> tuple:
    """The Literal choices of one field, or () if it is not an enum."""
    import typing

    field = output_model.model_fields.get(field_name)
    if field is None:
        return ()
    if typing.get_origin(field.annotation) is typing.Literal:
        return typing.get_args(field.annotation)
    return ()
