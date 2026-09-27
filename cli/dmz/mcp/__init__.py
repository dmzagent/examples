"""The platform's MCP server, from the client side: a JSON-RPC client for
``/mcp/v1`` and a stdio bridge that standard MCP hosts can launch."""
from .client import McpClient, McpError, READ_ONLY_TOOLS, unwrap  # noqa: F401
