import sys
from pathlib import Path

if __package__ in (None, ""):
    # Run as a folder (`python research_agent/ ...`): make the package importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research_agent.cli import main  # noqa: E402

sys.exit(main())
