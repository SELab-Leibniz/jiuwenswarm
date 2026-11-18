"""Native Jiuwen MCP tools for an externally selected read-only graph."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import memgraph


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admit", type=Path)
    args = parser.parse_args()
    if args.admit is not None:
        memgraph.admit(args.admit)
        return
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("memgraph-readonly")

    @server.tool()
    def team_memory_health() -> str:
        """Verify the exact frozen MemGraph release; never select another graph."""
        return memgraph.render(memgraph.request("/health"))

    @server.tool()
    def team_memory_query(query: str, top_k: int = 5) -> str:
        """Recall historical coding strategies by concrete symbols, mechanisms or errors."""
        return memgraph.query(query, top_k)

    @server.tool()
    def team_memory_evidence(memory_id: str, query: str) -> str:
        """Read source excerpts and provenance for a returned rule or episode ID."""
        return memgraph.evidence(memory_id, query)

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
