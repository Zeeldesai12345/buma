"""
Run the Buma MCP server over stdio:  uv run python -m buma.mcp_server

stdout is reserved for MCP protocol messages; all logging goes to stderr.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import anyio

from buma.mcp_server.server import create_server


def main() -> None:
    # MCP clients read the server's stderr as UTF-8; Windows would otherwise use the console code page.
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    server = create_server()
    # psycopg's async driver cannot run on Windows' default ProactorEventLoop.
    backend_options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}
    anyio.run(server.run_stdio_async, backend_options=backend_options)


if __name__ == "__main__":
    main()
