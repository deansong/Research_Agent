"""
WHAT:  Which models each provider will actually accept, asked of the provider.
WHY:   A model is named per role, and a typo or a retired id fails minutes into
       a turn with a message about the model. A list to pick from -- in the
       web UI's dropdowns and `main.py models` -- removes the guessing.
CONCEPT: Not LangGraph. One function per provider, the same shape as auth.py.

How each one answers, measured:

    codex         Codex().models()       the app-server's model/list; ~3s,
                                         includes which one is the default
    antigravity   agy models             "id<TAB>Display name" per line; ~2s,
                                         and exits 1 when signed out
    claude_code   (nothing)              the `claude` CLI has no command that
                                         lists models, so this is the aliases
                                         its --help documents plus full ids
    fake          (nothing)              one placeholder

Never raises. A provider that cannot answer comes back with an empty list and
an `error` saying why, and the caller still lets a person type a model by hand.
"""

from __future__ import annotations

import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

#: Long enough for codex to start its app-server and answer.
LIST_TIMEOUT = 30.0

#: Model lists change on the order of weeks, and asking costs a subprocess
#: and a network round trip, so one answer is kept this long per process.
CACHE_SECONDS = 600.0


@dataclass(frozen=True)
class ModelInfo:
    id: str
    """What goes in `model` -- the value --model / config.json takes."""
    label: str = ""
    default: bool = False
    """The provider's own default, when it says which one that is."""


@dataclass(frozen=True)
class ModelList:
    provider: str
    models: tuple[ModelInfo, ...] = field(default_factory=tuple)
    source: str = ""
    """Where the list came from, in words -- shown next to it."""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "models": [vars(m) for m in self.models],
            "source": self.source,
            "error": self.error,
        }


# ---- one function per provider --------------------------------------------


def _codex() -> ModelList:
    try:
        from openai_codex import Codex
    except ImportError:
        return ModelList("codex", error="The openai-codex package is not installed.")
    if not shutil.which("codex"):
        return ModelList("codex", error="`codex` is not on PATH.")

    with Codex() as client:
        listed = client.models().data
    models = tuple(
        ModelInfo(id=m.id, label=m.display_name or m.id, default=bool(m.is_default))
        for m in listed if not m.hidden
    )
    return ModelList("codex", models, source="codex app-server (model/list)")


def _antigravity(executable: str = "agy") -> ModelList:
    from agent.backends.auth import _run, _verdict

    if not shutil.which(executable):
        return ModelList("antigravity", error=f"`{executable}` is not on PATH.")

    code, out, err = _run([executable, "models"], scrub=True)
    if code != 0:
        why = _verdict(err, out).removeprefix("Error:").strip()
        return ModelList("antigravity",
                         error=why or f"`{executable} models` exited {code}.")

    models = []
    for line in out.splitlines():
        # "Fetching available models..." has no tab; every model line does.
        if "\t" not in line:
            continue
        model_id, _, label = line.partition("\t")
        if model_id.strip():
            models.append(ModelInfo(id=model_id.strip(), label=label.strip()))
    return ModelList("antigravity", tuple(models), source=f"`{executable} models`")


#: The `claude` CLI cannot list its models; `claude --help` documents aliases
#: for the latest of each family, and accepts a full model id as well. The
#: aliases come first because they keep working when a new version ships.
CLAUDE_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo("opus", "Opus (latest)"),
    ModelInfo("sonnet", "Sonnet (latest)"),
    ModelInfo("haiku", "Haiku (latest)"),
    ModelInfo("fable", "Fable (latest)"),
    ModelInfo("claude-opus-5-5", "Claude Opus 5.5"),
    ModelInfo("claude-sonnet-5", "Claude Sonnet 5"),
    ModelInfo("claude-haiku-4-5", "Claude Haiku 4.5"),
    ModelInfo("claude-fable-5-1", "Claude Fable 5.1"),
)


def _claude_code() -> ModelList:
    note = "built-in list: the `claude` CLI has no command that lists models"
    if not shutil.which("claude"):
        return ModelList("claude_code", CLAUDE_MODELS, source=note,
                         error="`claude` is not on PATH.")
    return ModelList("claude_code", CLAUDE_MODELS, source=note)


def _fake() -> ModelList:
    return ModelList("fake", (ModelInfo("fake", "canned answers, no model"),),
                     source="built in")


_LISTERS = {
    "codex": _codex,
    "antigravity": _antigravity,
    "claude_code": _claude_code,
    "fake": _fake,
}

_cache: dict[str, tuple[float, ModelList]] = {}
_cache_lock = threading.Lock()


def list_models(provider: str, *, refresh: bool = False) -> ModelList:
    """The models one provider offers. Never raises; see the module docstring."""
    lister = _LISTERS.get(provider)
    if lister is None:
        return ModelList(provider, error=f"Listing models is not supported for "
                                         f"'{provider}'; type the model id instead.")

    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(provider)
    if hit and not refresh and now - hit[0] < CACHE_SECONDS:
        return hit[1]

    try:
        result = lister()
    except Exception as exc:  # a probe: every failure is an answer
        result = ModelList(provider, error=f"{type(exc).__name__}: {exc}")

    # Failures are not cached: the usual one is "not logged in", and the
    # next thing a person does is log in and look again.
    if not result.error:
        with _cache_lock:
            _cache[provider] = (now, result)
    return result


def list_all(providers=None, *, refresh: bool = False) -> list[ModelList]:
    """Every provider at once, in parallel -- each one is a few seconds."""
    names = sorted(providers if providers is not None else _LISTERS)
    with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
        futures = [pool.submit(list_models, name, refresh=refresh) for name in names]
        out = []
        for name, future in zip(names, futures):
            try:
                out.append(future.result(timeout=LIST_TIMEOUT))
            except Exception as exc:
                out.append(ModelList(name, error=f"No answer: {exc}"))
        return out
