"""The cmux CLI, as the backend calls it."""

from __future__ import annotations

import asyncio

from ....errors import RunnerCoreError

# Shown with the error when the cmux CLI is missing or cmux cannot be reached (its socket refuses the caller); not when
# cmux answered with an error of its own, such as a browser page that stopped responding.
UNREACHABLE_HINT = "Run the server (pnpm dev:python) from a terminal inside the cmux app: cmux only accepts commands from processes started inside it."


async def cmux(*args: str) -> str:
    """One cmux CLI call: its output, or an error that says what cmux answered."""
    try:
        process = await asyncio.create_subprocess_exec("cmux", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError as error:
        raise RunnerCoreError("The cmux CLI is not on PATH.", code="cmux_unavailable", hint=UNREACHABLE_HINT) from error
    output = (await process.communicate())[0].decode().strip()
    if process.returncode != 0:
        unreachable = "socket" in output.lower()
        raise RunnerCoreError(f"cmux {args[0]} failed: {output[-300:]}", code="cmux_unavailable" if unreachable else "cmux_failed",
                              hint=UNREACHABLE_HINT if unreachable else None)
    return output
