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
    assert RB.list_providers() == ["antigravity", "claude_code", "codex", "codex_strata",
                                   "fake", "strata"]
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
        if model is sessions.Text:
            data = model(text=f"answer {len(self.calls)}")
        elif hasattr(model, "raw_schema"):
            from research_backend.json_schema import example

            data = model.model_validate(example(model.raw_schema))
        else:
            data = model.model_validate({"dataset": "imdb", "why": "free"})
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


def test_effort_is_set_kept_with_the_session_and_sent(monkeypatch):
    made = []

    def backend(provider, model, options, quiet=True, readable=()):
        made.append(dict(options))
        return Spy()

    monkeypatch.setattr(sessions, "_backend", backend)
    reader = RB(provider="fake", effort="high")
    assert repr(reader) == "RB(provider='fake', model=None, effort='high', session=None)"
    reader.send("one")
    reader.set_effort("low")
    reader.send("two")
    assert [m.get("effort") for m in made] == ["high", "low"]
    assert RB(session=reader.session_id).effort == "low", "saved with the session"
    assert "(low effort)" in str(reader.session)
    reader.set_effort(None)
    reader.send("three")
    assert "effort" not in made[-1], "None: the model's default"

    # Chosen before the provider: kept, and checked once there is one.
    assert RB(effort="xhigh").set_provider("claude_code").effort == "xhigh"
    with pytest.raises(RB.BackendUnavailable, match="claude_code has no effort level 'ultra'"):
        RB(effort="ultra").set_provider("claude_code")
    # Another provider has other levels, so a set effort does not follow.
    assert RB(provider="claude_code", effort="max").set_provider("fake").effort is None


def test_effort_levels_are_the_models_where_the_provider_says(monkeypatch):
    from research_backend import catalog

    listed = catalog.ModelList("codex", (
        catalog.ModelInfo("big", default=True, efforts=("low", "medium", "high", "ultra"),
                          default_effort="medium"),
        catalog.ModelInfo("small", efforts=("low", "medium", "high"))))
    monkeypatch.setattr(catalog, "list_models", lambda provider, refresh=False: listed)

    assert RB.list_efforts("codex") == ["low", "medium", "high", "ultra"], "the default model's"
    assert RB.list_efforts("codex", "small") == ["low", "medium", "high"]
    assert RB.list_efforts("claude_code") == ["low", "medium", "high", "xhigh", "max"]
    assert RB.list_efforts("antigravity") == ["low", "medium", "high", "xhigh", "max"]
    assert "big  (default)  effort: low, medium, high, ultra (default medium)" in str(listed)

    with pytest.raises(RB.BackendUnavailable, match="codex small has no effort level 'ultra'"):
        RB(provider="codex", model="small", effort="ultra")
    reader = RB(provider="codex", model="big", effort="ultra")
    with pytest.raises(RB.BackendUnavailable, match="small has no effort level 'ultra'"):
        reader.set_model("small")
    assert reader.model == "big", "refused before anything changed"
    assert reader.list_efforts() == ["low", "medium", "high", "ultra"]


def test_each_cli_is_given_the_effort(monkeypatch):
    from research_backend.backends import antigravity, claude_code

    monkeypatch.setattr("shutil.which", lambda name: f"/bin/{name}")
    claude = claude_code.ClaudeCodeBackend(effort="max")
    argv = claude.argv(prompt="x", instructions="", access=Access.WRITE,
                       schema_path=__file__, session=[])
    assert argv[argv.index("--effort") + 1] == "max"
    agy = antigravity.AntigravityBackend(effort="low")
    argv = agy.argv(prompt="x", instructions="", access=Access.NONE,
                    schema_path=__file__, session=[])
    assert argv[argv.index("--effort") + 1] == "low"
    assert "--effort" not in claude_code.ClaudeCodeBackend().argv(
        prompt="x", instructions="", access=Access.WRITE, schema_path=__file__, session=[])
    with pytest.raises(RB.BackendUnavailable, match="has no effort level 'ultra'"):
        claude_code.ClaudeCodeBackend(effort="ultra")


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


PAPERS = {
    "title": "Papers", "type": "object", "required": ["papers"],
    "properties": {"papers": {"type": "array", "items": {
        "type": "object", "required": ["title", "year", "kind"],
        "properties": {"title": {"type": "string"}, "year": {"type": "integer"},
                       "kind": {"type": "string", "enum": ["method", "dataset", "survey"]}}}}}}


def test_a_json_schema_from_a_dict_text_or_file_gives_a_dict(tmp_path):
    path = tmp_path / "papers.json"
    path.write_text(json.dumps(PAPERS))
    reader = RB(provider="fake")
    for schema in (PAPERS, json.dumps(PAPERS), str(path), path):
        reply = reader.send("two papers", schema=schema)
        assert reply.data == {"papers": [{"title": "[fake title #%d]" % reply.turn,
                                          "year": reply.turn, "kind": "method"}]}
        assert json.loads(reply.text) == reply.data
    assert reader.session.turns[0]["schema"] == PAPERS, "kept with the turn"


def test_set_schema_shapes_every_answer_until_cleared(spy):
    reader = RB(provider="fake").set_schema(PAPERS)
    reader.send("one")
    assert spy.calls[-1]["output_model"].raw_schema == PAPERS

    class Pick(BaseModel):
        dataset: str
        why: str

    assert isinstance(reader.send("two", schema=Pick).data, Pick), "per message wins"
    reader.set_schema(None)
    assert str(reader.send("three")) == "answer 3", "plain text again"


def test_a_json_schema_is_checked_both_ways():
    from pydantic import ValidationError

    from research_backend import json_schema
    from research_backend.backends._schema import strict_json_schema

    reader = RB(provider="fake")
    with pytest.raises(ValueError, match="top level must be"):
        reader.set_schema({"type": "array"})
    with pytest.raises(ValueError, match="not a valid JSON schema"):
        reader.set_schema({"type": "object", "properties": {"a": {"type": "strin"}}})
    with pytest.raises(ValueError, match="not valid JSON"):
        reader.set_schema("{not json")
    with pytest.raises(FileNotFoundError):
        reader.set_schema("/no/such/schema.json")

    model = json_schema.model_for(PAPERS)
    # What is SENT is the schema, made strict the way codex requires.
    sent = strict_json_schema(model)
    assert sent["additionalProperties"] is False
    assert sent["properties"]["papers"]["items"]["additionalProperties"] is False
    # What comes BACK is checked against the schema as written.
    good = model.model_validate_json('{"papers": [{"title": "LoRA", "year": 2021, "kind": "method"}]}')
    assert good.root["papers"][0]["title"] == "LoRA"
    with pytest.raises(ValidationError, match="papers/0/kind: 'blog' is not one of"):
        model.model_validate_json('{"papers": [{"title": "x", "year": 1, "kind": "blog"}]}')


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
    lines = str(listed).splitlines()
    assert lines[0].split() == ["id", "provider", "model", "effort", "turns", "last", "used",
                                "last", "message"]
    assert lines[1].split()[:5] == ["newer", "fake", "default", "default", "1"]
    assert lines[1].endswith("  b") and lines[2].startswith("older ")
    assert str(RB.list_sessions("codex")) == "(no sessions)"


def test_continue_close_and_delete(spy):
    reader = RB(provider="fake")
    reader.new_session("lit-review")
    reader.send("hello")
    with pytest.raises(ValueError, match=r"RB\(session='lit-review'\)"):
        RB(provider="fake").new_session("lit-review")

    again = RB(provider="fake")
    assert again.new_session("lit-review", exist_ok=True) == "lit-review"
    assert again.send("more").turn == 2, "continued, not started over"
    assert RB(provider="fake").new_session("fresh", exist_ok=True) == "fresh"
    with pytest.raises(ValueError, match="is on fake, not claude_code"):
        RB(provider="claude_code").new_session("lit-review", exist_ok=True)

    assert again.close_session() == "lit-review" and again.session_id is None
    assert again.send("new topic").turn == 1, "the next send starts a new session"
    assert RB(session="lit-review").send("back again").turn == 3, "closed, not gone"

    again.delete_session("lit-review")
    with pytest.raises(RB.SessionNotFound):
        RB(session="lit-review")


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


# ---- skills --------------------------------------------------------------------------


@pytest.fixture
def skills_dir(tmp_path, monkeypatch):
    """A folder of two skills, and no $RESEARCH_BACKEND_SKILLS_DIR."""
    monkeypatch.delenv("RESEARCH_BACKEND_SKILLS_DIR", raising=False)
    root = tmp_path / "skills"
    (root / "alpha").mkdir(parents=True)
    (root / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: >\n  First skill,\n  for testing.\nlicense: MIT\n---\n\n"
        "ALPHA BODY: run {python} {skill_dir}/tool.py in {workspace}, read {skills_dir}, "
        "keep \\usepackage{acl} and {artifacts_dir} as written.\n")
    (root / "beta").mkdir()
    (root / "beta" / "SKILL.md").write_text("---\ndescription: Second.\n---\nBETA BODY\n")
    (root / "notes").mkdir()                       # no SKILL.md: not a skill
    (root / "notes" / "README.md").write_text("x")
    return root


def test_skills_are_found_in_a_folder_and_listed(skills_dir):
    listed = RB.list_skills(skills_dir)
    assert listed.names == ["alpha", "beta"]
    assert listed[0].description == "First skill, for testing.", "YAML > block"
    assert listed[0].body.startswith("ALPHA BODY")
    text = str(RB(provider="fake", skills_dir=skills_dir, skills=["beta"]).list_skills())
    assert text.splitlines()[0] == f"2 skills in {skills_dir}"
    assert " * beta" in text and "   alpha" in text
    assert str(RB.list_skills()).startswith("(no skills folder")


def test_auto_lists_the_skills_and_chosen_ones_are_given_in_full(spy, skills_dir):
    auto = RB(provider="fake", skills_dir=skills_dir, skills="auto")
    auto.send("plan a study")
    first = spy.calls[-1]["prompt"]
    assert first.startswith("SKILLS YOU CAN USE")
    assert "alpha: First skill, for testing." in first
    assert str(skills_dir / "beta" / "SKILL.md") in first
    assert "ALPHA BODY" not in first and first.endswith("plan a study")

    chosen = RB(provider="fake", skills_dir=skills_dir, skills=["alpha"])
    chosen.send("go")
    sent = spy.calls[-1]["prompt"]
    assert sent.startswith("SKILLS FOR THIS TASK") and "BETA BODY" not in sent
    workdir = chosen.session.workdir
    assert f"run {sys.executable} {skills_dir / 'alpha'}/tool.py in {workdir}" in sent
    assert f"read {skills_dir}," in sent
    assert "\\usepackage{acl} and {artifacts_dir} as written" in sent, "others left alone"


def test_skills_go_with_the_first_message_and_again_only_when_changed(spy, skills_dir):
    reader = RB(provider="fake", skills_dir=skills_dir, skills=["alpha"])
    reader.send("one")
    reader.send("two")
    assert "ALPHA BODY" in spy.calls[0]["prompt"]
    assert spy.calls[1]["prompt"] == "two"
    assert spy.calls[0]["developer_instructions"] == "", "not in the instructions"

    reader.set_skills(["beta"])
    reader.send("three")
    assert "BETA BODY" in spy.calls[2]["prompt"] and spy.calls[2]["prompt"].endswith("three")

    later = RB(session=reader.session_id)
    assert later.skills == ["beta"] and later.skills_dir == str(skills_dir)
    later.send("four")
    assert spy.calls[3]["prompt"] == "four", "continued later: not sent again"

    turns = later.session.turns
    assert [t.get("skills") for t in turns] == [["alpha"], None, ["beta"], None]
    assert [t["prompt"] for t in turns] == ["one", "two", "three", "four"], "your words only"
    assert "SKILL" not in later.history()

    later.set_skills(None)
    later.send("five")
    assert spy.calls[4]["prompt"] == "five"


def test_the_skills_folder_is_readable_on_turns_with_skills(monkeypatch, skills_dir):
    made = []

    def backend(provider, model, options, quiet=True, readable=()):
        made.append(readable)
        return Spy()

    monkeypatch.setattr(sessions, "_backend", backend)
    RB(provider="fake", skills_dir=skills_dir, skills="auto").send("x")
    RB(provider="fake").send("y")
    assert made == [(str(skills_dir),), ()]

    # The real factory hands it to the backend, which Claude turns into --add-dir.
    from research_backend.backends import claude_code

    monkeypatch.undo()   # the real sessions._backend again
    monkeypatch.setattr("shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr("research_backend.backends.make",
                        lambda provider, model, **o: claude_code.ClaudeCodeBackend(model=model))
    claude = sessions._backend("claude_code", None, {}, readable=(str(skills_dir),))
    argv = claude.argv(prompt="x", instructions="", access=Access.READ_ONLY,
                       schema_path=__file__, session=[])
    assert argv[argv.index("--add-dir") + 1] == str(skills_dir)


def test_skills_need_a_folder_and_known_names(skills_dir, monkeypatch):
    with pytest.raises(ValueError, match="needs a skills folder"):
        RB(provider="fake", skills="auto")
    with pytest.raises(ValueError, match=r"No skill called 'gamma'.*It has: alpha, beta"):
        RB(provider="fake", skills_dir=skills_dir, skills=["alpha", "gamma"])
    with pytest.raises(FileNotFoundError):
        RB(provider="fake", skills_dir=skills_dir / "missing", skills="auto")
    reader = RB(provider="fake", skills_dir=skills_dir)
    with pytest.raises(ValueError, match="No skill called 'gamma'"):
        reader.set_skills(["gamma"])
    assert reader.skills is None
    with pytest.raises(ValueError, match="No skill called 'alpha'"):
        RB(provider="fake", skills_dir=skills_dir, skills=["alpha"]).set_skills_dir(
            skills_dir / "notes")

    monkeypatch.setenv("RESEARCH_BACKEND_SKILLS_DIR", str(skills_dir))
    from_env = RB(provider="fake", skills="beta")
    assert from_env.skills == ["beta"] and from_env.skills_dir == str(skills_dir)
    assert RB.list_skills().names == ["alpha", "beta"]


def test_the_repository_skills_folder_reads():
    root = pathlib.Path(__file__).resolve().parents[1] / "skills"
    listed = RB.list_skills(root)
    assert len(listed) >= 24 and all(s.description for s in listed)
    assert {"acl-paper", "statistical-power", "research"} <= set(listed.names)


def test_on_progress_reaches_the_turn_running_on_its_own_thread(tmp_path, monkeypatch):
    """A backend reports progress through its on_progress; send(on_progress=)
    gets it -- each concurrent turn its own, though they share one backend."""
    import threading

    class Talky:
        on_progress = None
        go = threading.Barrier(2)

        def run_structured(self, *, thread_id, repo_path, access, developer_instructions,
                           prompt, output_model):
            self.go.wait(5)                  # both turns are running now
            for n in range(3):
                self.on_progress({"events": [{"kind": "command", "phase": "started",
                                              "command": f"{prompt} {n}"}], "elapsed": n})
            return StructuredRun(data=output_model(text="ok"), thread_id="t", is_new_thread=True,
                                 usage=Usage(), events=[])

    shared = Talky()
    monkeypatch.setattr(sessions, "_backend", lambda *a, **k: shared)
    seen = {"a": [], "b": []}

    def turn(name):
        rb = RB("fake", home=tmp_path)
        rb.new_session(name)
        rb.send(name, on_progress=lambda snap: seen[name].append(snap["events"][0]["command"]))

    threads = [threading.Thread(target=turn, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert seen == {"a": ["a 0", "a 1", "a 2"], "b": ["b 0", "b 1", "b 2"]}
    shared.go = threading.Barrier(1)
    rb = RB("fake", home=tmp_path)
    rb.new_session("quiet")
    assert rb.send("no hook").text, "no callback: nothing breaks"


def test_the_newest_codex_binary_is_used(monkeypatch, tmp_path):
    """The SDK's bundled Codex can be older than the CLI on PATH, and Codex
    offers models by client version: the newer one is run."""
    from research_backend.backends import codex as C

    monkeypatch.delenv("RESEARCH_BACKEND_CODEX_BIN", raising=False)
    versions = {"/bundled/codex": (0, 147, 0), "/path/codex": (0, 153, 4)}
    monkeypatch.setattr(C, "_binary_version", lambda p: versions[p])
    monkeypatch.setattr("shutil.which", lambda name: "/path/codex")
    import types
    import sys
    monkeypatch.setitem(sys.modules, "codex_cli_bin",
                        types.SimpleNamespace(bundled_codex_path=lambda: "/bundled/codex"))
    assert C.codex_bin() == "/path/codex"
    versions["/path/codex"] = (0, 140, 0)
    assert C.codex_bin() is None, "an older one on PATH: the bundled one"
    monkeypatch.setenv("RESEARCH_BACKEND_CODEX_BIN", "/my/codex")
    assert C.codex_bin() == "/my/codex"
