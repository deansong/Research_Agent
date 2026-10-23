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

import research_backend as rb  # noqa: E402
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
    assert rb.providers() == ["antigravity", "claude_code", "codex", "fake"]
    assert callable(rb.providers), "not shadowed by a subpackage of the same name"


def test_models_can_be_looped_over_and_printed():
    listed = rb.models("fake")
    assert [m.id for m in listed] == ["fake"] and listed.ids == ["fake"]
    assert str(listed).startswith("fake: 1 model(s)\n  fake  canned answers")
    unknown = rb.models("nope")
    assert not len(unknown) and "not supported" in unknown.error


def test_status_says_how_to_log_in():
    assert str(rb.status("fake")).startswith("fake: logged in")
    out = rb.AuthStatus(provider="codex", installed=True, logged_in=False,
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
    quota = rb.usage("codex")
    assert asked == ["account/rateLimits/read"] and GetAccountRateLimitsResponse
    assert quota.ok and quota.plan == "team"
    assert [(w.name, w.used_percent) for w in quota.windows] == [("5 hours", 0.0),
                                                                 ("weekly", 5.0)]
    assert quota.credits == "no credits; 2 free limit resets available"
    assert quota.most_used.name == "weekly"
    text = str(quota)
    assert text.startswith("codex (team plan)\n  5 hours") and "5% used, resets" in text


def test_claude_limits_are_the_last_ones_a_turn_reported():
    assert "No limits seen yet" in rb.usage("claude_code").error
    usage.remember_claude_limits([{"kind": "command"},
                                  {"kind": "rate_limit_event", "info": CLAUDE_EVENT}])
    quota = rb.usage("claude_code")
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
    assert rb.usage("claude_code").most_used.name == "weekly"


def test_every_providers_usage_at_once_never_raises(monkeypatch):
    def broken():
        raise RuntimeError("app-server gone")

    monkeypatch.setitem(usage.READERS, "codex", broken)
    monkeypatch.setitem(usage.READERS, "antigravity", lambda: usage.Quota("antigravity"))
    report = rb.usage()
    assert [q.provider for q in report] == ["antigravity", "claude_code", "codex"]
    assert report[2].error == "RuntimeError: app-server gone"
    assert "codex\n  RuntimeError: app-server gone" in str(report)


# ---- sessions ---------------------------------------------------------------------


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
                             usage=Usage(input_tokens=10, output_tokens=2), events=[{"k": 1}])


@pytest.fixture
def spy(monkeypatch):
    backend = Spy()
    monkeypatch.setattr(sessions, "_backend", lambda *a, **k: backend)
    return backend


def test_a_session_is_continued_by_id_from_anywhere(spy):
    s = rb.Provider("fake").new_session(name="lit-review", instructions="Be brief.")
    first = s.send("hello")
    assert str(first) == "answer 1" and first.turn == 1 and first.tokens.input_tokens == 10
    assert spy.calls[0]["thread_id"] is None
    assert spy.calls[0]["developer_instructions"] == "Be brief."
    assert spy.calls[0]["access"] is Access.READ_ONLY
    assert spy.calls[0]["repo_path"] == str(s.folder / "workspace")

    # As another process would: by id, from the file.
    again = rb.session("lit-review").send("and then?")
    assert again.turn == 2 and spy.calls[1]["thread_id"] == "provider-thread-1"
    assert len(s.turns) == 1, "an object made earlier is stale..."
    s.send("third")
    assert spy.calls[2]["thread_id"] == "provider-thread-1" and len(s.turns) == 3, \
        "...until it sends, which reads the file first"

    saved = json.loads((rb.session("lit-review").folder / "session.json").read_text())
    assert [t["prompt"] for t in saved["turns"]] == ["hello", "and then?", "third"]
    assert saved["turns"][0]["tokens"] == {"input_tokens": 10, "output_tokens": 2}
    assert "you: and then?\nfake: answer 2" in rb.session("lit-review").history()


def test_a_schema_gets_a_structured_answer(spy):
    class Pick(BaseModel):
        dataset: str
        why: str

    reply = rb.Provider("fake").new_session().send("pick one", schema=Pick)
    assert isinstance(reply.data, Pick) and reply.data.dataset == "imdb"
    assert json.loads(reply.text) == {"dataset": "imdb", "why": "free"}


def test_a_failed_turn_is_recorded_and_raised(monkeypatch):
    class Down(Spy):
        def run_structured(self, **kwargs):
            raise rb.BackendBusy("overloaded")

    monkeypatch.setattr(sessions, "_backend", lambda *a, **k: Down())
    s = rb.Provider("fake").new_session(name="down")
    with pytest.raises(rb.BackendBusy):
        s.send("hello")
    turn = rb.session("down").turns[0]
    assert turn["error"] == "BackendBusy: overloaded" and rb.session("down").thread_id is None


def test_one_turn_at_a_time_per_session(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class Slow(Spy):
        def run_structured(self, **kwargs):
            entered.set()
            release.wait(5)
            return super().run_structured(**kwargs)

    monkeypatch.setattr(sessions, "_backend", lambda *a, **k: Slow())
    rb.Provider("fake").new_session(name="slow")
    worker = threading.Thread(target=lambda: rb.session("slow").send("first"))
    worker.start()
    assert entered.wait(5)
    with pytest.raises(rb.SessionBusy, match="answering another message"):
        rb.session("slow").send("second")
    release.set()
    worker.join(5)
    assert [t["prompt"] for t in rb.session("slow").turns] == ["first"]


def test_the_real_fake_provider_continues_its_conversation():
    s = rb.Provider("fake").new_session(name="echo")
    assert str(s.send("hello")).startswith("[fake] reply 1 in this conversation, to: hello")
    assert str(rb.session("echo").send("again")).startswith("[fake] reply 2 in this conversation")


def test_sessions_are_listed_newest_first_and_printed(spy):
    rb.Provider("fake").new_session(name="older").send("a")
    rb.Provider("fake").new_session(name="newer").send("b")
    listed = rb.sessions()
    assert [s.id for s in listed][:2] == ["newer", "older"]
    text = str(listed)
    assert text.startswith("newer: fake/default model, read_only access in ")
    assert "last: b -> answer" in text


def test_bad_names_missing_sessions_and_access_are_refused(tmp_path):
    with pytest.raises(ValueError, match="cannot be a session id"):
        rb.Provider("fake").new_session(name="../escape")
    rb.Provider("fake").new_session(name="taken")
    with pytest.raises(ValueError, match="open_session"):
        rb.Provider("fake").new_session(name="taken")
    with pytest.raises(rb.SessionNotFound, match="Sessions: taken"):
        rb.session("nobody")
    with pytest.raises(FileNotFoundError):
        rb.Provider("fake").new_session(workdir=tmp_path / "missing")
    with pytest.raises(rb.BackendUnavailable, match="cannot do read_only"):
        rb.Provider("antigravity", access="read_only")
    with pytest.raises(rb.BackendUnavailable, match="Unknown provider 'gpt'"):
        rb.Provider("gpt")
    # agy cannot do read_only, so by default it gets none, not a surprise.
    assert rb.Provider("antigravity").new_session().access is Access.NONE
    assert rb.Provider("fake").new_session(workdir=tmp_path).workdir == str(tmp_path)


def test_a_session_can_be_renamed_and_deleted(spy, tmp_path):
    s = rb.Provider("fake").new_session(name="draft")
    s.send("hello")
    s.rename("final")
    assert rb.session("final").workdir == str(s.folder / "workspace")
    assert rb.session("final").send("more").turn == 2
    with pytest.raises(rb.SessionNotFound):
        rb.session("draft")

    mine = rb.Provider("fake").new_session(name="mine", workdir=tmp_path)
    mine.delete()
    assert tmp_path.exists(), "a workdir you chose is left alone"
    rb.session("final").delete()
    assert [s.id for s in rb.sessions()] == []


def test_providers_side_by_side_each_with_its_own_sessions(spy, tmp_path):
    codex = rb.Provider("codex", model="gpt-5.6-sol", instructions="Read papers.")
    claude = rb.Provider("claude_code", model="opus", access="write")
    assert repr(claude) == "Provider('claude_code', model='opus', access='write')"

    reader = codex.new_session("lit-review")
    writer = claude.new_session("draft", instructions="You write LaTeX.")
    scratch = codex.new_session("scratch", model="gpt-5.6-luna", access="none")
    reader.send("find datasets")
    writer.send("draft it")
    scratch.send("quick question")

    assert [(c["developer_instructions"], c["access"]) for c in spy.calls] == [
        ("Read papers.", Access.READ_ONLY), ("You write LaTeX.", Access.WRITE),
        ("Read papers.", Access.NONE)]
    assert (reader.model, writer.model, scratch.model) == ("gpt-5.6-sol", "opus",
                                                            "gpt-5.6-luna")
    assert scratch.access is Access.NONE, "a session may differ from its provider"

    assert {s.id for s in codex.sessions()} == {"lit-review", "scratch"}
    assert [s.id for s in claude.sessions()] == ["draft"]
    assert codex.session("lit-review").send("more").turn == 2
    with pytest.raises(ValueError, match="is a claude_code session, not codex"):
        codex.session("draft")
    assert {s.id for s in rb.sessions()} == {"lit-review", "scratch", "draft"}

    # A Provider can keep its sessions somewhere of its own.
    elsewhere = rb.Provider("fake", home=tmp_path / "project-a")
    elsewhere.new_session("lit-review").send("same id, another place")
    assert (tmp_path / "project-a" / "sessions" / "lit-review" / "session.json").exists()
    assert rb.session("lit-review", home=tmp_path / "project-a").provider == "fake"
    assert rb.session("lit-review").provider == "codex"


def test_a_quiet_backend_prints_nothing_and_a_watched_one_does(capsys):
    from research_backend.base import say

    class Backend:
        quiet = True

    say(Backend(), "hidden")
    Backend.quiet = False
    say(Backend(), "shown")
    assert capsys.readouterr().out == "shown\n"

    quiet = sessions._backend("fake", None, {}, quiet=True)
    watched = sessions._backend("fake", None, {}, quiet=False)
    assert quiet is not watched and quiet.quiet and not watched.quiet
    assert sessions._backend("fake", None, {}) is quiet


def test_a_provider_answers_for_itself():
    fake = rb.Provider("fake")
    assert fake.models().ids == ["fake"]
    assert fake.usage().ok and fake.status().ok


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
