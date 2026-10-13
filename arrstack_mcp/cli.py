"""Command-line entry point (`arrstack-mcp`, `python -m arrstack_mcp`, `python server.py`).

Settings are loaded when arrstack_mcp.server is imported, so `--config PATH`
is turned into $ARRSTACK_CONFIG first; the server then reads it the same way
whether it was started as a script, a module or an installed command.
"""

import os
import sys


def main() -> None:
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--config" and i + 1 < len(args):
            os.environ["ARRSTACK_CONFIG"] = args[i + 1]
            os.environ["_ARRSTACK_CONFIG_EXPLICIT"] = "1"
        elif arg.startswith("--config="):
            os.environ["ARRSTACK_CONFIG"] = arg.split("=", 1)[1]
            os.environ["_ARRSTACK_CONFIG_EXPLICIT"] = "1"
    from arrstack_mcp import server

    server.main()


if __name__ == "__main__":
    main()
