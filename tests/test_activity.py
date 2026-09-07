"""
WHAT:  Tests for the record of what a provider actually did.
WHY:   This is the answer to "313 events, last one 7s ago" -- and the events it
       keeps are exactly the ones the terminal throws away, so nothing else in
       the suite would notice if it stopped keeping them.
CONCEPT: No LangGraph, no model. Structured records and files.

Run with:   python tests/test_activity.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent import activity  # noqa: E402
from agent.backends._progress import MAX_OUTPUT, describe, record  # noqa: E402


class _Item:
    """Stands in for one SDK thread item. Matched by CLASS NAME, as the real
    code does -- the SDK wraps everything in a ThreadItem RootModel, so the
    name is all there is to go on."""

    def __init__(self, name, **fields):
        self.__class__ = type(name, (_Item,), {})
        for key, value in fields.items():
            setattr(self, key, value)


def _event(notification: str, item):
    payload = type(notification, (), {"item": item})()
    return type("Event", (), {"payload": payload})()


def test_record_keeps_what_describe_throws_away():
    """The whole point, in one test.

    describe() answers "what is one line worth printing?" and returns None for
    a successful command -- so a terminal shows the command starting and never
    shows the result. record() has to keep the output, because that is the part
    you want an hour later.
    """
    item = _Item("CommandExecutionThreadItem",
                 command="python -m pytest -q",
                 exit_code=0,
                 aggregated_output="4 passed in 0.31s\n")
    event = _event("ItemCompletedNotification", item)

    assert describe(event) is None, "a successful command prints nothing -- by design"

    kept = record(event)
    assert kept["kind"] == "command"
    assert kept["exit_code"] == 0
    assert "4 passed" in kept["output"], kept
    print("PASS  a successful command's output is kept even though it is not printed")


def test_record_keeps_every_reasoning_line_and_prints_the_latest():
    """The live line shows the LAST summary; the record keeps them all.

    The last one rather than the first is deliberate: on a progressive update
    the tail is the new part, and on completion it is the conclusion. Either
    way it is the bit you have not already read. The record still has the
    whole sequence, which is what you want an hour later.
    """
    item = _Item("ReasoningThreadItem",
                 summary=["first thought", "second thought", "third thought"])
    event = _event("ItemCompletedNotification", item)

    line = describe(event)
    assert "third thought" in line, line
    assert "first thought" not in line, "one line, not the whole history"

    kept = record(event)
    assert kept["summary"] == ["first thought", "second thought", "third thought"]
    print("PASS  the newest reasoning line prints; all of them are recorded")


def test_huge_output_is_clipped_from_the_middle():
    """A pytest run can be megabytes. Clipping the MIDDLE keeps both the
    command's first output and its final failure, which are the two ends you
    actually read."""
    item = _Item("CommandExecutionThreadItem", command="big",
                 exit_code=1, aggregated_output="A" * 200 + "B" * 20_000 + "Z" * 200)
    kept = record(_event("ItemCompletedNotification", item))

    assert len(kept["output"]) < MAX_OUTPUT + 200, len(kept["output"])
    assert kept["output"].startswith("A"), "the beginning must survive"
    assert kept["output"].endswith("Z"), "the end must survive"
    assert "characters omitted" in kept["output"]
    print("PASS  oversized output is clipped from the middle, keeping both ends")


def test_an_unknown_event_is_recorded_rather_than_dropped():
    """How you find out the SDK grew a new item type. describe() returns None
    for anything it does not know, which is silent by design; a bare name in
    the record is enough to notice."""
    kept = record(_event("ItemCompletedNotification", _Item("SomethingNewThreadItem")))
    assert kept == {"kind": "something_new", "phase": "completed"}, kept
    print("PASS  an unrecognised event is recorded by name")


def test_turns_are_written_and_read_back_newest_first():
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(3):
            activity.write(tmp, "worker", [
                {"kind": "command", "phase": "completed", "command": f"run {i}"},
            ])

        found = activity.turns(tmp, "worker")
        assert [t.index for t in found] == [3, 2, 1], [t.index for t in found]
        assert activity.latest(tmp, "worker").index == 3
        assert activity.nodes_with_activity(tmp) == ["worker"]
        print("PASS  turns are numbered, appended, and read back newest first")


def test_counts_do_not_double_started_completed_pairs():
    """A command emits two events describing ONE command. Counting both makes
    every total silently twice what happened."""
    with tempfile.TemporaryDirectory() as tmp:
        activity.write(tmp, "worker", [
            {"kind": "command", "phase": "started", "command": "ls"},
            {"kind": "command", "phase": "completed", "command": "ls", "exit_code": 0},
            {"kind": "reasoning", "phase": "completed", "summary": ["hm"]},
        ])
        counts = activity.latest(tmp, "worker").counts()
        assert counts == {"command": 1, "reasoning": 1}, counts
        print("PASS  a started/completed pair counts once")


def test_writing_never_raises():
    """A node's real work must not fail because a log about it could not be
    written -- the failure would arrive as a mysterious node error rather than
    a missing file."""
    assert activity.write("/proc/nonexistent/nope", "worker", [{"kind": "x"}]) is None
    assert activity.write("", "worker", [{"kind": "x"}]) is None
    assert activity.write("/tmp", "worker", []) is None, "nothing to write is not an error"
    print("PASS  a failed or empty write returns None instead of raising")


def test_the_fake_backend_emits_events_so_this_is_testable_offline():
    """A test double that omits the interesting channel is not much of a double.

    Without these, the activity record could only ever be exercised against a
    real paid provider -- so the most valuable new output in the system would
    have had no offline test at all.
    """
    from pydantic import BaseModel

    from agent.backends.base import Access
    from agent.backends.fake import FakeBackend

    class Out(BaseModel):
        summary: str = ""

    run = FakeBackend().run_structured(
        thread_id=None, repo_path=".", access=Access.READ_ONLY,
        developer_instructions="", prompt="", output_model=Out,
    )
    kinds = {e["kind"] for e in run.events}
    assert {"command", "reasoning", "file_change", "message"} <= kinds, kinds
    assert any(e.get("exit_code") for e in run.events), "a failing command is worth having"
    assert any(e.get("output") for e in run.events), "output is the point"
    print(f"PASS  the fake backend emits {len(run.events)} events across {len(kinds)} kinds")



def test_nothing_is_silently_invisible_any_more():
    """The complaint that prompted this: "137 events, last one 11s ago".

    describe() knew three notification types and six item kinds while the
    stream carries far more, so most of a long turn produced no output at all
    -- and because record() matched separately, the same events were missing
    from the record too. Both now come from one place, and an unknown kind
    gets a line with its name on it rather than silence.
    """
    for notification in ("ItemUpdatedNotification", "SomethingBrandNew"):
        event = _event(notification, _Item("MysteryThreadItem"))
        kept = record(event)
        assert kept is not None, f"{notification} recorded nothing"
        assert kept["kind"] == "mystery", kept
        assert describe(event), f"{notification} printed nothing"

    # A few kinds are deliberately quiet, because they arrive constantly and
    # say nothing -- that is a decision, not an oversight.
    from agent.backends._progress import headline
    assert headline({"kind": "token_count", "phase": "other"}) is None
    assert headline({"kind": "turn_started", "phase": "other"}) is None
    print("PASS  unknown events are visible; only the noisy kinds stay quiet")


def test_heartbeat_says_what_is_happening():
    """The line itself. Three numbers and no information was the whole
    problem, so the replacement has to carry a breakdown and the last thing
    that happened."""
    from agent.backends.codex import _heartbeat

    line = _heartbeat(55, 11, 137,
                      {"reasoning": 96, "command": 22, "file_change": 12},
                      "· normalising the scoring table per role")
    assert "96 reasoning" in line, line
    assert "22 command" in line, line
    assert "quiet 11s" in line, line
    assert "normalising the scoring table" in line, line

    # Long text is trimmed: this repeats every thirty seconds.
    long_line = _heartbeat(55, 11, 1, {"reasoning": 1}, "x" * 400)
    assert len(long_line.splitlines()[1]) < 120, long_line
    print("PASS  the heartbeat reports kinds and the last thing that happened")

if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nAll activity tests passed.")
