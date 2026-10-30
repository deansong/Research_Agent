"""The Strata backends, offline: `strata` against a local server that speaks
the Responses API's stream, and `codex_strata`'s profile read into overrides."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pydantic
import pytest

import research_backend as RB
from research_backend.backends import responses


class _Server(BaseHTTPRequestHandler):
    """Answers every turn with `reply`, streamed; keeps what it was sent."""

    seen: list[dict] = []
    reply = '{"answer": 42}'
    status = 200

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append({"body": body, "auth": self.headers.get("Authorization")})
        if type(self).status != 200:
            self.send_response(type(self).status)
            self.end_headers()
            self.wfile.write(b'{"error": "no"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        text = type(self).reply
        events = [
            {"type": "response.created", "response": {}},
            {"type": "response.reasoning_text.delta", "delta": "thinking"},
            {"type": "response.output_item.done", "item": {
                "type": "reasoning", "content": [{"type": "reasoning_text", "text": "thinking"}]}},
            {"type": "response.output_text.delta", "delta": text},
            {"type": "response.output_item.done", "item": {
                "type": "message", "content": [{"type": "output_text", "text": text}]}},
            {"type": "response.completed", "response": {"usage": {
                "input_tokens": 10, "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 2}}}},
        ]
        for event in events:
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
        self.wfile.flush()

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"data": [{"id": "qwen-x"}]}).encode())

    def log_message(self, *args):
        pass


@pytest.fixture()
def server(monkeypatch, tmp_path):
    _Server.seen, _Server.reply, _Server.status = [], '{"answer": 42}', 200
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Server)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("STRATA_BASE_URL", f"http://127.0.0.1:{httpd.server_port}/v1")
    monkeypatch.setenv("STRATA_API_KEY", "test-key")
    monkeypatch.setenv("RESEARCH_BACKEND_HOME", str(tmp_path / "rbhome"))
    from research_backend import conversation
    conversation._backends.clear()
    yield _Server
    httpd.shutdown()


class Out(pydantic.BaseModel):
    answer: int
    note: str = ""


def test_a_turn_and_a_continued_one(server, tmp_path):
    rb = RB("strata", home=tmp_path / "home", instructions="Be terse.")
    snaps = []
    reply = rb.send("17 + 25?", schema=Out, on_progress=snaps.append)
    assert reply.data.answer == 42
    assert reply.tokens.input_tokens == 10 and reply.tokens.reasoning_tokens == 2
    first = server.seen[0]
    assert first["auth"] == "Bearer test-key"
    assert first["body"]["instructions"] == "Be terse."
    assert first["body"]["model"] == "strata"
    fmt = first["body"]["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"]
    assert fmt["schema"]["required"] == ["answer", "note"]
    assert [e["kind"] for e in reply.events] == ["reasoning", "message"]

    server.reply = '```json\n{"answer": 43}\n```'
    assert rb.send("plus one", schema=Out).data.answer == 43
    sent = server.seen[1]["body"]["input"]
    # The server keeps nothing: the conversation so far is sent again.
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[0]["content"].endswith("17 + 25?") and sent[1]["content"] == '{"answer": 42}'


def test_plain_text_asks_for_no_format(server, tmp_path):
    server.reply = "Eleven is prime."
    assert RB("strata", home=tmp_path).send("a prime?").text == "Eleven is prime."
    assert "text" not in server.seen[0]["body"]


def test_no_files_and_no_key(server, tmp_path, monkeypatch):
    with pytest.raises(RB.BackendUnavailable, match="read_only"):
        RB("strata", access="read_only", home=tmp_path).send("hi")
    monkeypatch.delenv("STRATA_API_KEY")
    with pytest.raises(RB.BackendUnavailable, match="STRATA_API_KEY"):
        RB("strata", home=tmp_path).send("hi")


def test_a_refused_key_says_so(server, tmp_path):
    server.status = 401
    with pytest.raises(RB.BackendUnavailable, match="refused the key"):
        RB("strata", home=tmp_path).send("hi")


def test_an_answer_off_the_schema_is_an_output_error(server, tmp_path):
    server.reply = "not json"
    with pytest.raises(RB.BackendOutputError):
        RB("strata", home=tmp_path).send("hi", schema=Out)


def test_models_listed(server):
    assert [i for i, _ in responses.list_models()] == ["strata", "qwen-x"]


def test_old_turns_are_left_out_when_the_window_is_full():
    backend = responses.StrataBackend(context_window=100)        # ~150 characters
    history = [{"role": "user", "content": "a" * 60}, {"role": "assistant", "content": "b" * 60},
               {"role": "user", "content": "c" * 20}, {"role": "assistant", "content": "d" * 20}]
    sent = backend._fit(history, {"role": "user", "content": "now"}, "")
    assert [m["content"][0] for m in sent] == ["c", "d", "n"]


def test_the_codex_profile_becomes_overrides(tmp_path, monkeypatch):
    from research_backend.backends import codex

    (tmp_path / "strata.config.toml").write_text(
        'model = "strata"\nmodel_provider = "strata"\nmodel_context_window = 131072\n'
        '[model_providers.strata]\nbase_url = "http://h:1/v1"\nwire_api = "responses"\n'
        'env_key = "STRATA_API_KEY"\n[tui]\nx = true\n[features]\napps = true\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert codex.profile_overrides("strata") == (
        'model="strata"', 'model_provider="strata"', "model_context_window=131072",
        'model_providers.strata.base_url="http://h:1/v1"',
        'model_providers.strata.wire_api="responses"',
        'model_providers.strata.env_key="STRATA_API_KEY"')
    assert codex.profile_model("strata") == "strata"
    with pytest.raises(RB.BackendError, match="No Codex profile"):
        codex.profile_overrides("nope")


def test_codex_providers_get_their_profiles_client(monkeypatch):
    from research_backend import backends

    asked = []
    monkeypatch.setattr(backends, "codex_client", lambda profile=None: asked.append(profile) or object())
    assert backends.make("codex_strata").name == "codex_strata"
    backends.make("codex")
    assert asked == ["strata", None]


def test_the_ui_keeps_file_roles_off_text_only_providers():
    from research_agent.web.host import FILE_ROLES, _reaches_files

    assert not _reaches_files("strata")
    assert _reaches_files("codex_strata") and _reaches_files("codex")
    assert "validator" in FILE_ROLES["execution"] and "researcher" in FILE_ROLES["bootstrap"]


def test_claude_code_is_pointed_at_the_strata_server(monkeypatch):
    # Measured: the Strata server answers Anthropic's /v1/messages, and the
    # claude CLI runs on it with these variables -- set after the scrub, so a
    # parent Claude session's ANTHROPIC_* cannot leak in.
    from research_backend.backends import _cli, load
    from research_backend.base import BackendUnavailable

    cls = load("claude_strata")
    backend = cls()
    assert backend.model == "strata", "always named: the CLI would ask for a Claude model"
    monkeypatch.setenv("STRATA_API_KEY", "k-test")
    monkeypatch.setenv("STRATA_BASE_URL", "http://strata.example:21435/v1/")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "the-parent-session-s")
    env = backend.child_env()
    assert env["ANTHROPIC_BASE_URL"] == "http://strata.example:21435", "the CLI adds /v1/messages"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "k-test"
    assert {env[k] for k in env if k.endswith("_MODEL")} == {"strata"}

    seen = {}

    def popen(argv, env=None, **kwargs):
        seen.update(argv=argv, env=env)
        raise OSError("stop here")

    monkeypatch.setattr(_cli.subprocess, "Popen", popen)
    try:
        backend._run(["claude", "-p"], "/tmp", stdin_text="hi")
    except Exception:  # noqa: BLE001 -- only the environment it was given matters
        pass
    assert seen["env"]["ANTHROPIC_AUTH_TOKEN"] == "k-test"
    assert "ANTHROPIC_API_KEY" not in seen["env"], "the parent's key is scrubbed"
    assert "--model" in backend.argv(prompt="", instructions="", access=__import__(
        "research_backend.base", fromlist=["Access"]).Access.READ_ONLY,
        schema_path=__file__, session=[])
    usage = backend.usage_from({"usage": {"input_tokens": 3, "output_tokens": 2},
                                "total_cost_usd": 0.08})
    assert usage.cost_usd is None, "the CLI prices Strata's tokens as Anthropic's"

    monkeypatch.delenv("STRATA_API_KEY")
    with pytest.raises(BackendUnavailable, match="STRATA_API_KEY"):
        backend.child_env()
