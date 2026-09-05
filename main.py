"""
WHAT:  The entry point. Deliberately tiny.
WHY:   One thing has to happen BEFORE langgraph is imported anywhere, and this
       is the only file guaranteed to run first.
CONCEPT: Checkpoint serialisation.

LANGGRAPH_STRICT_MSGPACK makes LangGraph refuse to save anything it does not
positively recognise, instead of falling back to a permissive encoder. That
turns "this object silently became a dict three sessions ago" into an error at
the moment you write it. Worth the strictness while learning.

If you add another entry point (say `python -m agent`), it needs this same
setdefault before its own imports.
"""

import os

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from agent.cli import main  # noqa: E402  (must come after the env var above)


if __name__ == "__main__":
    main()
