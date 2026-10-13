"""Run arrstack-mcp from a source checkout: `python server.py [options]`.

The code lives in the arrstack_mcp package; installed copies also provide the
`arrstack-mcp` command.
"""

from arrstack_mcp.cli import main

if __name__ == "__main__":
    main()
