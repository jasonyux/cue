"""The history MCP server: the history index (storage/search.py) for an agent that takes its tools over MCP.

A standalone agent starts it over stdio (``servers.py``) and gets search_history, the same tool the proposer has: what
the person did before, from every session, by keywords within a time slice, for a detail of the task the agent is
unsure of. The index is the backend's own file, under the data root ``CUA_DATA_ROOT`` names (the server's config sets
it); both open it, and the index serializes its catch-ups, so neither indexes a line twice. Of MCP it answers
initialize, ping, tools/list and tools/call. The results stay before the run started (``CUA_HISTORY_BEFORE``, from the
server's config): the run's own question goes on the stream as the run starts, and a search is of what came before.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from ....config import Settings
from ...storage.search import TOOL, TOOL_NAME, HistoryIndex, run_tool
from ...storage.stream import Stream

LINE_LIMIT = 1024 * 1024  # one request line


def index() -> HistoryIndex:
    """The backend's index, from the data root the environment names."""
    memory = Settings.from_environment().data_root / "memory"
    return HistoryIndex(memory / "search.sqlite", Stream(memory / "stream"))


def answer(history: HistoryIndex, request: dict[str, Any]) -> dict[str, Any]:
    """The JSON-RPC reply to one request, without its id: a result, or an error for a method this server lacks."""
    method, params = request.get("method"), request.get("params") or {}
    if method == "initialize":
        return {"result": {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                           "serverInfo": {"name": "computerpilot-history", "version": "1"}}}
    if method == "ping":
        return {"result": {}}
    if method == "tools/list":
        return {"result": {"tools": [{"name": TOOL_NAME, "description": TOOL["function"]["description"], "inputSchema": TOOL["function"]["parameters"]}]}}
    if method == "tools/call":
        if params.get("name") != TOOL_NAME:
            return {"result": {"content": [{"type": "text", "text": f"Unknown tool {params.get('name')!r}: only {TOOL_NAME} exists."}], "isError": True}}
        try:
            text, _note = run_tool(history, params.get("arguments"), before=os.environ.get("CUA_HISTORY_BEFORE") or None)
            return {"result": {"content": [{"type": "text", "text": text}]}}
        except Exception as error:  # noqa: BLE001 - every failure goes back to the agent as the call's result
            return {"result": {"content": [{"type": "text", "text": f"{type(error).__name__}: {error}"}], "isError": True}}
    return {"error": {"code": -32601, "message": f"Method not found: {method}"}}


async def serve() -> None:
    """Answer requests from stdin, one at a time, until the agent closes it."""
    history = index()
    reader = asyncio.StreamReader(limit=LINE_LIMIT)
    await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    try:
        while line := await reader.readline():
            request = json.loads(line)
            if "id" in request:  # a notification wants no answer
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], **answer(history, request)}) + "\n")
                sys.stdout.flush()
    finally:
        history.close()


if __name__ == "__main__":
    asyncio.run(serve())
