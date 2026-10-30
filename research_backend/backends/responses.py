"""
WHAT:  A backend for a server that speaks OpenAI's Responses API
       (POST <base_url>/responses), and `strata`, the Strata server.
WHY:   The cheapest way to run the roles that only think about text -- the
       discussor, the orchestrator, the planner -- on a model served on the
       network, without an agent CLI in between.
CONCEPT: An adapter, over plain HTTP (stdlib only). Like `api` was meant to
       be, it has NO files and NO tools: max_access is NONE, so a role that
       needs to read or change files cannot be given to it.

How a turn goes:
    - developer_instructions -> `instructions`; the conversation so far and
      the prompt -> `input`, as messages;
    - a schema -> `text.format` json_schema, strict (the same tightened schema
      Codex gets, see _schema.py); plain text asks for no format;
    - the reply streams (Server-Sent Events), so on_progress sees reasoning
      and the answer as they are written, and a silent server is noticed.

The server keeps nothing (store=false): the conversation is resent every turn,
from <research_backend home>/responses_threads/<thread>.json. When it grows
past what the model can read, the oldest turns are left out of what is sent
(not out of the file).
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from research_backend import home as _home
from research_backend.backends._progress import LIVE_FLUSH_SECONDS, _LiveText
from research_backend.backends._schema import strict_json_schema
from research_backend.base import (Access, BackendBusy, BackendCancelled, BackendError,
                                   BackendOutputError, BackendTimeout, BackendUnavailable,
                                   OutputT, StructuredRun, Usage, checked_effort)

#: Roughly how many characters a token is, to keep a resent conversation
#: inside the model's window.
_CHARS_PER_TOKEN = 3


class ResponsesBackend:
    """One model on a Responses-API server. Text in, text or JSON out."""
    INSTRUCTIONS_ROUTE = "the Responses API `instructions`, with every message"
    PROVIDER_ADDS = "nothing: the request is exactly what is shown"

    name = "responses"
    supports_repo_access = False
    max_access = Access.NONE
    #: Reading files is not something it can do, so a call that names no
    #: access gets NONE (see conversation._access).
    unsupported_access = frozenset({Access.READ_ONLY, Access.WRITE, Access.FULL})
    efforts: tuple[str, ...] = ("none", "low", "medium", "high")

    #: Defaults a named server (a subclass) fills in.
    default_base_url = ""
    default_model = ""
    key_env = "OPENAI_API_KEY"
    base_url_env = ""
    context_window = 128_000

    def __init__(self, *, model: str | None = None, effort: str | None = None,
                 base_url: str | None = None, api_key_env: str | None = None,
                 timeout: float = 600.0, max_seconds: float = 3600.0,
                 max_output_tokens: int | None = None, context_window: int | None = None,
                 **_ignored):
        self.model = model or self.default_model
        if not self.model:
            raise BackendUnavailable(f"{self.name}: name a model.")
        self.effort = checked_effort(self.name, effort, self.efforts)
        self.base_url = (base_url or (os.environ.get(self.base_url_env) if self.base_url_env
                                      else None) or self.default_base_url).rstrip("/")
        if not self.base_url:
            raise BackendUnavailable(f"{self.name}: give a base_url.")
        self.key_env = api_key_env or self.key_env
        self.timeout = float(timeout)
        """Seconds of silence from the server before the turn is abandoned."""
        self.max_seconds = float(max_seconds)
        self.max_output_tokens = max_output_tokens
        self.context_window = int(context_window or self.context_window)
        self.on_progress = None
        self.cancel = None
        self.pause = None
        self.quiet = True
        self._threads_lock = threading.Lock()

    # ---- the contract ----------------------------------------------------------------

    def run_structured(self, *, thread_id: str | None, repo_path: str, access: Access,
                       developer_instructions: str, prompt: str,
                       output_model: type[OutputT]) -> StructuredRun[OutputT]:
        thread = thread_id or f"{self.name}-{uuid.uuid4().hex[:12]}"
        history = self._load(thread) if thread_id else []
        text_only = set(getattr(output_model, "model_fields", {})) == {"text"}
        message = {"role": "user", "content": prompt}
        body: dict = {"model": self.model, "input": self._fit(history, message,
                                                              developer_instructions),
                      "stream": True, "store": False}
        if developer_instructions:
            body["instructions"] = developer_instructions
        if not text_only:
            body["text"] = {"format": {"type": "json_schema", "name": "answer",
                                       "strict": True,
                                       "schema": strict_json_schema(output_model)}}
        if self.effort:
            body["reasoning"] = {"effort": self.effort}
        if self.max_output_tokens:
            body["max_output_tokens"] = int(self.max_output_tokens)

        answer, usage, events = self._stream(body)
        if text_only:
            data = output_model.model_validate({"text": answer})
        else:
            try:
                data = output_model.model_validate_json(_unfenced(answer))
            except Exception as exc:  # noqa: BLE001 - pydantic or json
                raise BackendOutputError(
                    f"{self.name} answered with something that does not match the "
                    f"schema: {exc}\n\nIts answer began: {answer[:500]!r}") from exc

        self._save(thread, history + [message, {"role": "assistant", "content": answer}])
        return StructuredRun(data=data, thread_id=thread, is_new_thread=thread_id is None,
                             usage=usage, events=events)

    def close(self) -> None:
        return None

    # ---- the HTTP turn ---------------------------------------------------------------

    def _request(self, body: dict) -> urllib.request.Request:
        key = os.environ.get(self.key_env, "")
        if not key:
            raise BackendUnavailable(f"{self.name}: set {self.key_env} to the server's "
                                     f"API key (in the environment of this process).")
        return urllib.request.Request(
            f"{self.base_url}/responses", data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "Accept": "text/event-stream"})

    def _stream(self, body: dict) -> tuple[str, Usage | None, list[dict]]:
        """Send one turn and read its stream: (answer text, usage, events)."""
        try:
            response = urllib.request.urlopen(self._request(body), timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise BackendUnavailable(f"{self.name}: cannot reach {self.base_url}: "
                                     f"{getattr(exc, 'reason', exc)}") from exc

        live = _LiveText()
        events: list[dict] = []
        answer: list[str] = []
        usage = None
        started = time.monotonic()
        last_event = started
        flushed = 0.0
        with response:
            while True:
                if self.cancel is not None and self.cancel.is_set():
                    raise BackendCancelled(f"{self.name}: the turn was cancelled.")
                if time.monotonic() - started > self.max_seconds:
                    raise BackendTimeout(f"{self.name}: no answer within "
                                         f"{self.max_seconds:.0f}s.")
                try:
                    line = response.readline()
                except (socket.timeout, TimeoutError) as exc:
                    raise BackendTimeout(f"{self.name}: the server said nothing for "
                                         f"{self.timeout:.0f}s.") from exc
                if not line:
                    break
                line = line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                last_event = time.monotonic()
                kind = event.get("type", "")
                if kind == "response.output_text.delta":
                    live.add("message", event.get("delta", ""))
                elif kind in ("response.reasoning_text.delta",
                              "response.reasoning_summary_text.delta"):
                    live.add("reasoning", event.get("delta", ""))
                elif kind == "response.output_item.done":
                    item = event.get("item") or {}
                    recorded = _record(item)
                    if recorded:
                        events.append(recorded)
                    if item.get("type") == "message":
                        answer.append(_text_of(item))
                elif kind in ("response.completed", "response.incomplete"):
                    final = event.get("response") or {}
                    usage = _usage(final.get("usage"), self.context_window)
                    if not answer:
                        answer = [_text_of(i) for i in final.get("output") or []
                                  if i.get("type") == "message"]
                    if kind == "response.incomplete":
                        why = (final.get("incomplete_details") or {}).get("reason", "")
                        events.append({"kind": "error", "phase": "completed",
                                       "message": f"incomplete: {why}"})
                    break
                elif kind in ("response.failed", "error"):
                    error = (event.get("response") or {}).get("error") or event.get("error") \
                        or event
                    raise BackendError(f"{self.name}: the turn failed: "
                                       f"{error.get('message', error) if isinstance(error, dict) else error}")
                now = time.monotonic()
                if self.on_progress is not None and now - flushed >= LIVE_FLUSH_SECONDS:
                    flushed = now
                    try:
                        self.on_progress({"events": list(events), "elapsed": now - started,
                                          "idle": now - last_event,
                                          "counts": {"events": len(events)},
                                          "last": live.tail(), "streamed": live.total(),
                                          "live": live.snapshot()})
                    except Exception:  # noqa: BLE001 - progress never breaks a turn
                        pass
        text = "".join(answer).strip()
        if not text:
            raise BackendOutputError(f"{self.name}: the server sent no answer.")
        return text, usage, events

    def _http_error(self, exc: urllib.error.HTTPError) -> BackendError:
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001
            detail = ""
        if exc.code in (401, 403):
            return BackendUnavailable(f"{self.name}: the server refused the key in "
                                      f"{self.key_env} ({exc.code}): {detail}")
        if exc.code in (429, 502, 503, 504):
            return BackendBusy(f"{self.name}: {exc.code} {detail}")
        return BackendError(f"{self.name}: {exc.code} {detail}")

    # ---- the conversation, kept here because the server keeps nothing --------------

    def _fit(self, history: list[dict], message: dict, instructions: str) -> list[dict]:
        """The conversation to send: the newest turns that fit in about half
        the window, then the new message."""
        budget = self.context_window * _CHARS_PER_TOKEN // 2
        budget -= len(instructions or "") + len(message["content"])
        kept: list[dict] = []
        # Whole turns (a question and its answer), newest first.
        for i in range(len(history) - 2, -1, -2):
            pair = history[i:i + 2]
            size = sum(len(m["content"]) for m in pair)
            if size > budget:
                break
            budget -= size
            kept[:0] = pair
        return kept + [message]

    def _path(self, thread: str) -> Path:
        return _home.home() / "responses_threads" / f"{thread}.json"

    def _load(self, thread: str) -> list[dict]:
        path = self._path(thread)
        if not path.is_file():
            raise BackendError(f"{self.name}: no conversation {thread} in {path.parent}; "
                               f"it cannot be continued.")
        return json.loads(path.read_text(encoding="utf-8")).get("messages", [])

    def _save(self, thread: str, messages: list[dict]) -> None:
        path = self._path(thread)
        with self._threads_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"thread": thread, "provider": self.name,
                                       "model": self.model, "messages": messages},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)


class StrataBackend(ResponsesBackend):
    """The Strata server: a Qwen model behind a Responses API. Its key is read
    from STRATA_API_KEY; STRATA_BASE_URL moves it."""

    name = "strata"
    default_base_url = "http://143.167.8.120:21435/v1"
    base_url_env = "STRATA_BASE_URL"
    default_model = "strata"
    key_env = "STRATA_API_KEY"
    context_window = 131_072


# ---- small helpers ----------------------------------------------------------------


def _text_of(item: dict) -> str:
    return "".join(part.get("text", "") for part in item.get("content") or []
                   if part.get("type") in ("output_text", "text"))


def _record(item: dict) -> dict | None:
    """An output item as the activity record's shape (see _progress.record)."""
    kind = item.get("type")
    if kind == "reasoning":
        lines = [s.get("text", "") for s in item.get("summary") or []] or \
                [c.get("text", "") for c in item.get("content") or []]
        lines = [line.strip() for line in lines if line and line.strip()]
        return {"kind": "reasoning", "phase": "completed", "summary": lines} if lines else None
    if kind == "message":
        text = _text_of(item).strip()
        return {"kind": "message", "phase": "completed", "text": text[:4000],
                "is_final_json": text.startswith("{")} if text else None
    return {"kind": str(kind or "item"), "phase": "completed"}


def _usage(raw: dict | None, window: int) -> Usage | None:
    if not raw:
        return None
    return Usage(input_tokens=raw.get("input_tokens", 0) or 0,
                 cached_input_tokens=(raw.get("input_tokens_details") or {}).get(
                     "cached_tokens", 0) or 0,
                 output_tokens=raw.get("output_tokens", 0) or 0,
                 reasoning_tokens=(raw.get("output_tokens_details") or {}).get(
                     "reasoning_tokens", 0) or 0,
                 context_window=window)


def _unfenced(text: str) -> str:
    """A JSON answer, without the ```json fence a model sometimes adds."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def list_models(backend_cls=StrataBackend) -> list[tuple[str, str]]:
    """(id, label) of what the server serves; its default model first."""
    key = os.environ.get(backend_cls.key_env, "")
    if not key:
        raise BackendUnavailable(f"set {backend_cls.key_env}")
    base = ((os.environ.get(backend_cls.base_url_env) if backend_cls.base_url_env else None)
            or backend_cls.default_base_url).rstrip("/")
    request = urllib.request.Request(f"{base}/models",
                                     headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(request, timeout=20) as response:
        listed = json.loads(response.read().decode()).get("data", [])
    out = [(backend_cls.default_model, f"{backend_cls.default_model} (the server's model)")]
    out += [(m["id"], m["id"]) for m in listed if m.get("id") and
            m["id"] != backend_cls.default_model]
    return out
