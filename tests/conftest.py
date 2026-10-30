"""Tests never read your own prompts (~/.research_agent/prompts, or
$RESEARCH_AGENT_PROMPTS): yours may differ from the defaults. Set
before anything is imported -- the prompt modules read their files then."""

import os
import tempfile

os.environ.pop("RESEARCH_AGENT_PROMPTS", None)
os.environ["RESEARCH_AGENT_HOME"] = tempfile.mkdtemp(prefix="research_agent_home_")
