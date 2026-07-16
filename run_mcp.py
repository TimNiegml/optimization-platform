#!/usr/bin/env python
"""Launch the L2 MCP server (so an agent can drive the platform).

    python run_mcp.py            # stdio transport  (Claude Desktop / Cursor / 本地 agent)
    python run_mcp.py --http     # streamable-HTTP transport on http://127.0.0.1:8765/mcp

Same as `python -m optplat.mcp_server`; kept next to run_graph.py / run_demo.py
so all entry points live together.
"""
from optplat.mcp_server import main

if __name__ == "__main__":
    main()
