"""Rya MCP surface.

`ops` holds plain, testable functions (no MCP dependency). `server` wraps them
in a FastMCP server (requires the [mcp] extra). Keeping the logic in `ops` means
the tools are unit-testable without the transport, and the `mcp` package stays
optional.
"""

from . import ops

__all__ = ["ops"]
