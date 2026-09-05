import os

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from agent.cli import main


if __name__ == "__main__":
    main()
