"""
WHAT:  research_backend as a package of its own: providers(), models(),
       usage(), status(), and sessions you can send to, continue from another
       process, and read back.
WHY:   It is the layer everything else will be rebuilt on, so what it
       promises -- one call per question, a conversation that survives the
       process, answers that print readably -- is pinned here, offline.

Run with:   python -m pytest tests/test_research_backend.py -q
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import research_backend as RB  # noqa: E402
from research_backend import conversation as sessions, limits as usage  # noqa: E402
from research_backend.base import Access, StructuredRun, Usage  # noqa: E402

# What the providers really said, 2026-10-23 -- the shapes the readers parse.
AGY_QUOTA = (
    "Gemini Models\tWeekly Limit Remaining\t94%\t2026-10-26T19:23:10Z\n"
    "Gemini Models\tFive Hour Limit Remaining\t100%\t2026-10-23T13:25:47Z\n"
    "Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t81%\t2026-10-25T10:17:24Z\n")

CLAUDE_EVENT = {
    "status": "allowed", "resetsAt": 1792761600, "rateLimitType": "five_hour",
    "overageStatus": "rejected", "isUsingOverage": False,
    "unifiedWindows": {"five_hour": {"utilization": 0.2, "resetsAt": 1792761600},
                       "seven_day": {"utilization": 0.41, "resetsAt": 1792998000}}}

CODEX_LIMITS = {
    "rate_limit_reset_credits": {"available_count": 2, "credits": []},
    "rate_limits": {
        "credits": {"balance": None, "has_credits": False, "unlimited": False},
        "limit_id": "codex", "plan_type": "team",
        "primary": {"resets_at": 1792761943, "used_percent": 0, "window_duration_mins": 300},
        "secondary": {"resets_at": 1793294223, "used_percent": 5,
                      "window_duration_mins": 10080},
        "rate_limit_reached_type": None, "spend_control_reached": False},
    "rate_limits_by_limit_id": None}


# ---- what is there ---------------------------------------------------------------


def test_the_providers_are_the_ones_you_can_send_to():
    assert RB.list_providers() == ["antigravity", "claude_code", "codex", "fake"]
    assert RB().list_providers() == RB.list_providers()


def test_models_can_be_looped_over_and_printed():
    listed = RB.list_models("fake")
    assert [m.id for m in listed] == ["fake"] and listed.ids == ["fake"]
    assert str(listed).startswith("fake: 1 model(s)\n  fake  canned answers")
    unknown = RB.list_models("nope")
    assert not len(unknown) and "not supported" in unknown.error


def test_status_says_how_to_log_in():
    assert str(RB.status("fake")).startswith("fake: logged in")
    out = RB.AuthStatus(provider="codex", installed=True, logged_in=False,
                        detail="Not logged in.", login_command="codex login")
    assert str(out) == "codex: NOT logged in\n  to log in: codex login"


# ---- usage --------------------------------------------------------------------------


def test_agy_quota_lines_become_windows_of_used_percent():
    windows = usage.parse_agy_quota("Fetching...\n" + AGY_QUOTA)
    assert [(w.name, w.used_percent) for w in windows] == [
        ("Gemini Models: Weekly Limit", 6.0),
        ("Gemini Models: Five Hour Limit", 0.0),
        ("Gemini 4 Argon: Weekly Limit", 19.0)]
    assert windows[0].left_percent == 94.0
    assert windows[0].resets_at.isoformat() == "2026-10-26T19:23:10+00:00"


def test_codex_limits_are_read_from_its_app_server(monkeypatch):
    from openai_codex.generated.v2_all import GetAccountRateLimitsResponse

    asked = []

    class Client:
        def request(self, method, params, response_model):
            asked.append(method)
            return response_model.model_validate(CODEX_LIMITS)

    class Codex:
        _client = Client()

    monkeypatch.setattr("shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr("research_backend.backends.codex_client", lambda: Codex())
    quota = RB.usage("codex")
    assert asked == ["account/rateLimits/read"] and GetAccountRateLimitsResponse
    assert quota.ok and quota.plan == "team"
    assert [(w.name, w.used_percent) for w in quota.windows] == [("5 hours", 0.0),
                                                                 ("weekly", 5.0)]
    assert quota.credits == "no credits; 2 free limit resets available"
    assert quota.most_used.name == "weekly"
    text = str(quota)
    assert text.startswith("codex (team plan)\n  5 hours") and "5% used, resets" in text


def test_claude_limits_are_the_last_ones_a_turn_reported():
    assert "No limits seen yet" in RB.usage("claude_code").error
    usage.remember_claude_limits([{"kind": "command"},
                                  {"kind": "rate_limit_event", "info": CLAUDE_EVENT}])
    quota = RB.usage("claude_code")
    assert quota.ok and quota.checked_at is not None
    assert [(w.name, w.used_percent, w.status) for w in quota.windows] == [
        ("5 hours", 20.0, "allowed"), ("weekly", 41.0, "")]


def test_a_claude_turn_keeps_the_limits_it_was_told(monkeypatch):
    from research_backend.backends import claude_code

    monkeypatch.setattr("shutil.which", lambda name: f"/bin/{name}")
    backend = claude_code.ClaudeCodeBackend()
    assert backend.record_for({"type": "rate_limit_event", "rate_limit_info": CLAUDE_EVENT}) \
        == {"kind": "rate_limit_event", "phase": "other", "info": CLAUDE_EVENT}

    run = StructuredRun(data=sessions.Text(text="hi"), thread_id="t", is_new_thread=True,
                        usage=None, events=[{"kind": "rate_limit_event", "info": CLAUDE_EVENT}])
    monkeypatch.setattr(claude_code.CliBackend, "run_structured", lambda self, **kw: run)
    assert backend.run_structured(thread_id=None, repo_path=".", access=Access.READ_ONLY,
                                  developer_instructions="", prompt="x",
                                  output_model=sessions.Text) is run
    assert RB.usage("claude_code").most_used.name == "weekly"


def test_every_providers_usage_at_once_never_raises(monkeypatch):
    def broken():
        raise RuntimeError("app-server gone")

    monkeypatch.setitem(usage.READERS, "codex", broken)
    monkeypatch.setitem(usage.READERS, "antigravity", lambda: usage.Quota("antigravity"))
    report = RB.usage()
    assert [q.provider for q in report] == ["antigravity", "claude_code", "codex"]
    assert report[2].error == "RuntimeError: app-server gone"
    assert "codex\n  RuntimeError: app-server gone" in str(report)


# ---- RB(...): one provider, one model, the current session -------------------------


class Spy:
    """A backend that remembers what it was asked."""

    name = "fake"
    max_access = Access.FULL

    def __init__(self):
        self.calls = []

    def run_structured(self, **kwargs):
        self.calls.append(kwargs)
        model = kwargs["output_model"]
        data = (model(text=f"answer {len(self.calls)}") if model is sessions.Text
                else model.model_validate({"dataset": "imdb", "why": "free"}))
        return StructuredRun(data=data, thread_id=kwargs["thread_id"] or "provider-thread-1",
                             is_new_thread=kwargs["thread_id"] is None,
                             usage=Usage(input_tokens=1500, cached_input_tokens=1000,
                                         output_tokens=2, context_window=10_000),
                             events=[{"k": 1}])


@pytest.fixture
def spy(monkeypatch):
    backend = Spy()
    monkeypatch.setattr(sessions, "_backend", lambda *a, **k: backend)
    return backend


def test_the_module_is_called_to_make_one():
    reader = RB(provider="fake", model="m1")
    assert isinstance(reader, RB.ResearchBackend)
    assert repr(reader) == "RB(provider='fake', model='m1', session=None)"
    empty = RB()
    assert empty.provider is None and empty.session_id is None
    with pytest.raises(ValueError, match="No provider chosen"):
        empty.send("hello")
    with pytest.raises(RB.BackendUnavailable, match="Unknown provider 'gpt'"):
        RB(provider="gpt")


def test_choose_then_start_a_session_then_talk(spy):
    reader = RB()
    reader.set_provider("fake")
    assert reader.list_models().ids == ["fake"] and reader.usage().ok and reader.status().ok
    assert reader.new_session("lit-review") == "lit-review" == reader.session_id
    first = reader.send("hello")
    assert str(first) == "answer 1" and first.turn == 1
    assert spy.calls[0]["thread_id"] is None and spy.calls[0]["access"] is Access.READ_ONLY
    assert spy.calls[0]["repo_path"] == str(reader.session.folder / "workspace")
    assert reader.send("and then?").turn == 2
    assert spy.calls[1]["thread_id"] == "provider-thread-1", "the same conversation"
    assert "you: and then?\nfake: answer 2" in reader.history()


def test_send_starts_a_session_when_there_is_none(spy):
    writer = RB(provider="fake")
    writer.send("hello")
    assert writer.session_id.startswith("fake-") and len(writer.session.turns) == 1


def test_a_session_is_continued_by_id_from_anywhere(spy):
    RB(provider="fake", instructions="Be brief.").new_session("lit-review")
    RB(session="lit-review").send("hello")              # as another script would
    later = RB(session="lit-review")
    assert later.provider == "fake" and later.send("more").turn == 2
    assert [c["developer_instructions"] for c in spy.calls] == ["Be brief.", "Be brief."]
    assert spy.calls[1]["thread_id"] == "provider-thread-1"

    reader = RB(provider="fake")
    reader.new_session("other")
    assert reader.open_session("lit-review") == "lit-review" == reader.session_id
    assert reader.send("third").turn == 3

    saved = json.loads((later.session.folder / "session.json").read_text())
    assert [t["prompt"] for t in saved["turns"]] == ["hello", "more", "third"]
    with pytest.raises(ValueError, match="is on fake, not codex"):
        RB(provider="codex", session="lit-review")
    with pytest.raises(RB.SessionNotFound, match="Sessions: "):
        RB(session="nobody")


def test_several_side_by_side_each_in_its_own_session(spy):
    codex = RB(provider="codex", model="gpt-5.6-sol", instructions="Read papers.")
    claude = RB(provider="claude_code", model="opus", access="write")
    codex.new_session("lit-review")
    claude.new_session("draft")
    codex.send("find datasets")
    claude.send("draft it")
    assert [(c["developer_instructions"], c["access"]) for c in spy.calls] == [
        ("Read papers.", Access.READ_ONLY), ("", Access.WRITE)]
    assert (codex.session.model, claude.session.model) == ("gpt-5.6-sol", "opus")
    assert [s.id for s in RB.list_sessions("codex")] == ["lit-review"]
    assert {s.id for s in RB.list_sessions()} == {"lit-review", "draft"}


def test_changing_model_access_or_instructions_carries_the_session_on(spy):
    reader = RB(provider="fake", model="small")
    reader.new_session("s")
    reader.send("one")
    reader.set_model("large").set_instructions("Be thorough.").set_access("write")
    reader.send("two")
    assert spy.calls[1]["thread_id"] == "provider-thread-1"
    assert (spy.calls[1]["developer_instructions"], spy.calls[1]["access"]) == (
        "Be thorough.", Access.WRITE)
    assert RB(session="s").model == "large", "saved with the session"

    reader.set_provider("claude_code")
    assert reader.session_id is None, "a session belongs to one provider"
    assert reader.model is None
    with pytest.raises(RB.BackendUnavailable, match="cannot do read_only"):
        RB(provider="antigravity", access="read_only")
    assert RB(provider="antigravity").new_session() and \
        RB.list_sessions("antigravity")[0].access is Access.NONE


def test_context_says_how_full_the_window_is(spy):
    reader = RB(provider="fake")
    assert "no session yet" in str(reader.context())
    reader.send("hello")
    context = reader.context()
    assert (context.tokens, context.window, context.percent) == (1500, 10_000, 15.0)
    text = str(context)
    assert text.startswith(f"{reader.session_id}: fake/default model, 1 turn(s)\n"
                           "context: about 1,500 of 10,000 tokens (15%)")
    assert "you: hello" in text


def test_tokens_in_context_are_counted_the_way_each_provider_reports_them():
    from research_backend.client import context_tokens

    codex = {"input_tokens": 15857, "cached_input_tokens": 14080}
    claude = {"input_tokens": 6, "cached_input_tokens": 38284}
    assert context_tokens("codex", codex) == 15857, "cached is inside input"
    assert context_tokens("claude_code", claude) == 38290, "cached is apart"


def test_a_schema_gets_a_structured_answer(spy):
    class Pick(BaseModel):
        dataset: str
        why: str

    reply = RB(provider="fake").send("pick one", schema=Pick)
    assert isinstance(reply.data, Pick) and reply.data.dataset == "imdb"
    assert json.loads(reply.text) == {"dataset": "imdb", "why": "free"}


def test_a_failed_turn_is_recorded_and_raised(monkeypatch):
    class Down(Spy):
        def run_structured(self, **kwargs):
            raise RB.BackendBusy("overloaded")

    monkeypatch.setattr(sessions, "_backend", lambda *a, **k: Down())
    reader = RB(provider="fake")
    reader.new_session("down")
    with pytest.raises(RB.BackendBusy):
        reader.send("hello")
    again = RB(session="down").session
    assert again.turns[0]["error"] == "BackendBusy: overloaded" and again.thread_id is None


def test_one_turn_at_a_time_per_session(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class Slow(Spy):
        def run_structured(self, **kwargs):
            entered.set()
            release.wait(5)
            return super().run_structured(**kwargs)

    monkeypatch.setattr(sessions, "_backend", lambda *a, **k: Slow())
    RB(provider="fake").new_session("slow")
    worker = threading.Thread(target=lambda: RB(session="slow").send("first"))
    worker.start()
    assert entered.wait(5)
    with pytest.raises(RB.SessionBusy, match="answering another message"):
        RB(session="slow").send("second")
    release.set()
    worker.join(5)
    assert [t["prompt"] for t in RB(session="slow").session.turns] == ["first"]


def test_the_real_fake_provider_continues_its_conversation():
    reader = RB(provider="fake")
    assert str(reader.send("hello")).startswith("[fake] reply 1 in this conversation, to: hello")
    assert str(reader.send("again")).startswith("[fake] reply 2 in this conversation")


def test_sessions_are_listed_newest_first_and_printed(spy):
    RB(provider="fake").new_session("older")
    RB(session="older").send("a")
    newer = RB(provider="fake")
    newer.new_session("newer")
    newer.send("b")
    listed = RB.list_sessions()
    assert [s.id for s in listed] == ["newer", "older"]
    text = str(listed)
    assert text.startswith("newer: fake/default model, read_only access in ")
    assert "last: b -> answer" in text


def test_bad_names_and_workdirs_are_refused(tmp_path):
    reader = RB(provider="fake")
    with pytest.raises(ValueError, match="cannot be a session id"):
        reader.new_session("../escape")
    reader.new_session("taken")
    with pytest.raises(ValueError, match="already a session called 'taken'"):
        RB(provider="fake").new_session("taken")
    with pytest.raises(FileNotFoundError):
        reader.new_session(workdir=tmp_path / "missing")
    reader.new_session("mine", workdir=tmp_path)
    assert reader.session.workdir == str(tmp_path)


def test_sessions_can_be_kept_elsewhere_renamed_and_deleted(spy, tmp_path):
    elsewhere = RB(provider="fake", home=tmp_path / "project-a")
    elsewhere.new_session("lit-review")
    elsewhere.send("hello")
    assert (tmp_path / "project-a" / "sessions" / "lit-review" / "session.json").exists()
    assert [s.id for s in RB.list_sessions(home=tmp_path / "project-a")] == ["lit-review"]
    assert not RB.list_sessions(), "not in the default place"

    reader = RB(provider="fake")
    reader.new_session("draft")
    reader.send("hello")
    reader.session.rename("final")
    assert RB(session="final").session.workdir == str(reader.session.folder / "workspace")
    assert RB(session="final").send("more").turn == 2

    mine = RB(provider="fake", workdir=tmp_path)
    mine.new_session("mine")
    mine.delete_session()
    assert mine.session_id is None and tmp_path.exists(), "a workdir you chose stays"
    RB().delete_session("final")
    assert list(RB.list_sessions()) == []


# ---- the agent still finds everything where it was ---------------------------------


def test_the_old_import_paths_are_the_same_modules():
    import agent.backends.base
    import agent.backends.codex
    import agent.backends.models
    from agent.backends import PROVIDERS

    import research_backend.backends.codex
    import research_backend.base
    import research_backend.catalog

    assert agent.backends.codex is research_backend.backends.codex
    assert agent.backends.base is research_backend.base
    assert agent.backends.models is research_backend.catalog
    assert PROVIDERS["fake"] == "agent.backends.fake:FakeBackend"
    assert PROVIDERS["codex"] == "research_backend.backends.codex:CodexBackend"
