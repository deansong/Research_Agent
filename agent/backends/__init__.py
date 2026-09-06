"""
WHAT:  The backend registry and factory -- turns config into live backends.
WHY:   One place that knows every provider name, and the one place that
       checks a role has been given a backend capable of doing its job.
CONCEPT: Composition root helper.  cli.py calls build_backends() once, then
       hands the result to build_graph(), which hands each node its own.
"""

from __future__ import annotations

from typing import Mapping

from agent.backends.base import Access, AgentBackend, BackendUnavailable

# provider name -> the class implementing it.
# Adding a provider = write the module, add one line here, done.
PROVIDERS: dict[str, str] = {
    "codex": "agent.backends.codex:CodexBackend",
    "claude_code": "agent.backends.claude_code:ClaudeCodeBackend",
    "api": "agent.backends.api:ApiBackend",
    "antigravity": "agent.backends.antigravity:AntigravityBackend",
    "fake": "agent.backends.fake:FakeBackend",
}


def _load(provider: str):
    """Import a provider class lazily.

    Lazily, so that configuring only Codex does not require the anthropic or
    openai packages to be installed.
    """
    import importlib

    if provider not in PROVIDERS:
        known = ", ".join(sorted(PROVIDERS))
        raise BackendUnavailable(f"Unknown provider '{provider}'. Known providers: {known}")

    module_path, class_name = PROVIDERS[provider].split(":")
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def build_backends(
    cfg,
    needed: Mapping[str, Access],
    *,
    codex_client=None,
) -> dict[str, AgentBackend]:
    """Create one backend per role, and check each can do that role's job.

    `needed` maps a role name to the access that role requires, e.g.
        {"discussor": Access.READ_ONLY, "executor": Access.WRITE}

    It is a PARAMETER rather than a lookup in roles.REQUIRED_ACCESS because
    role names are no longer a fixed set: a generated agent folder invents its
    own node names and declares each node's `access` in nodes.json. Passing the
    requirement in means the check is driven by what the agent actually asks
    for, which is strictly better than a hardcoded table.

    Returns a dict keyed by role name, e.g.
        {"discussor": <CodexBackend>, "executor": <CodexBackend>, ...}

    Two roles configured identically share ONE instance (see `cache` below),
    so four Codex roles do not open four clients.
    """
    from agent.config import backend_for

    cache: dict[tuple, AgentBackend] = {}
    built: dict[str, AgentBackend] = {}

    for role, required in needed.items():
        spec = backend_for(cfg, role)

        # The cache key is everything that distinguishes one instance from
        # another. options is turned into a sorted tuple so it can be hashed.
        key = (spec.provider, spec.model, tuple(sorted(spec.options.items())))

        # Check access BEFORE constructing anything. max_access is a class
        # attribute, so "the executor cannot run on a chat API" is answerable
        # without importing an SDK or opening a connection -- and the error
        # you get names the real problem instead of whatever the backend's
        # constructor happened to complain about first.
        _check_access(role, _load(spec.provider), spec, required)

        if key not in cache:
            cache[key] = _instantiate(spec, role=role, codex_client=codex_client)

        built[role] = cache[key]

    return built


def _instantiate(spec, *, role: str, codex_client):
    cls = _load(spec.provider)

    try:
        if spec.provider == "codex":
            # Codex needs the shared client that cli.py opened as a context
            # manager, so it is passed positionally rather than built here.
            if codex_client is None:
                raise BackendUnavailable(
                    "The codex backend needs a Codex client. This is a wiring bug "
                    "in cli.py, not a configuration problem."
                )
            return cls(codex_client, model=spec.model)

        return cls(model=spec.model, **spec.options)

    except BackendUnavailable as exc:
        # Re-raise with the role attached, so the message says WHICH role you
        # need to change rather than just naming the provider.
        raise BackendUnavailable(
            f"Role '{role}' is configured to use the '{spec.provider}' backend.\n\n{exc}"
        ) from None


def _check_access(role: str, backend_cls, spec, needed: Access) -> None:
    """Refuse, at startup, to give a role a backend that cannot do its job.

    This is the concrete answer to "can I run the executor on a plain chat
    API?" -- no, and you find out here rather than after the discussor and
    planner have already spent tokens.
    """
    if needed <= backend_cls.max_access:
        return

    if needed is Access.WRITE and not backend_cls.supports_repo_access:
        detail = (
            f"the '{spec.provider}' backend has no filesystem access at all, so it "
            f"cannot edit files"
        )
    else:
        detail = (
            f"the '{spec.provider}' backend tops out at {backend_cls.max_access.value} access"
        )

    raise BackendUnavailable(
        f"Config error: role '{role}' needs {needed.value} access to the "
        f"repository, but {detail}.\n"
        f"Use a backend that can, e.g. --backend-role {role}=codex"
    )
