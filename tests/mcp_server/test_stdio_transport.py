"""
stdio smoke tests: launch `python -m buma.mcp_server` as a real subprocess (N1 / DD-26).

stdio MCP puts protocol messages on stdout, so a single stray print corrupts the session. These
tests speak raw JSON-RPC and assert that EVERY stdout line is a JSON-RPC message, and that logging
went to stderr. No database is needed: the server only connects on the first query, and the
unreachable URL below also exercises error sanitization in a real process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import LATEST_PROTOCOL_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
UNREACHABLE_DB = "postgresql+psycopg://buma:sup3r-secret-pw@127.0.0.1:1/buma"
TIMEOUT_S = 30


def _server_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "BUMA_MCP_DATABASE_URL")}
    env["BUMA_MCP_DATABASE_URL"] = UNREACHABLE_DB
    return env


class StdioServer:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "buma.mcp_server"],
            cwd=REPO_ROOT,
            env=_server_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.stdout_lines: list[str] = []

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def read_response(self, request_id: int) -> dict:
        """Read stdout lines until the response for `request_id` arrives (with a hard timeout)."""
        result: dict = {}

        def _read() -> None:
            for line in self.proc.stdout:
                self.stdout_lines.append(line)
                message = json.loads(line)
                if message.get("id") == request_id:
                    result.update(message)
                    return

        reader = threading.Thread(target=_read, daemon=True)
        reader.start()
        reader.join(TIMEOUT_S)
        if not result:
            self.proc.kill()
            pytest.fail(f"no response to request {request_id}; stderr:\n{self.proc.stderr.read()}")
        return result

    def close(self) -> str:
        self.proc.stdin.close()
        try:
            remaining_stdout, stderr = self.proc.communicate(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            remaining_stdout, stderr = self.proc.communicate()
        self.stdout_lines.extend(line for line in remaining_stdout.splitlines(keepends=True) if line.strip())
        return stderr


def test_stdout_carries_only_jsonrpc_and_logs_go_to_stderr() -> None:
    server = StdioServer()
    server.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "buma-smoke-test", "version": "0"},
            },
        }
    )
    init = server.read_response(1)
    server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    server.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = server.read_response(2)
    server.send({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    resources = server.read_response(3)
    stderr = server.close()

    assert init["result"]["serverInfo"]["name"] == "buma"
    assert sorted(t["name"] for t in tools["result"]["tools"]) == ["get_triage_history", "get_workload"]
    assert [r["uri"] for r in resources["result"]["resources"]] == ["buma://repos"]

    # Every byte on stdout is protocol traffic
    assert server.stdout_lines, "no stdout at all"
    for line in server.stdout_lines:
        assert json.loads(line)["jsonrpc"] == "2.0", line

    # Logging went to stderr, and never printed the database URL
    assert "Buma MCP server ready" in stderr
    assert "sup3r-secret-pw" not in stderr


async def test_full_round_trip_through_the_sdk_stdio_client() -> None:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "buma.mcp_server"], env=_server_env(), cwd=REPO_ROOT
    )
    async with Client(params) as client:
        tools = (await client.list_tools()).tools
        result = await client.call_tool("get_workload", {"repo_id": 111})

    assert {t.name for t in tools} == {"get_triage_history", "get_workload"}
    # The database is unreachable: the client gets a clean error, never the connection string.
    text = " ".join(getattr(b, "text", "") for b in result.content)
    assert result.is_error
    assert "The Buma database is unavailable" in text
    assert "sup3r-secret-pw" not in text and "127.0.0.1" not in text
