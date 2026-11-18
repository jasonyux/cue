"""The live actor's remote desktop providers, by the name in remote_desktop.provider.

A provider gives the actor a second runtime with the local one's cell interface (``start``, ``capture``, ``close``,
``cell``), reached over the network. ``cua`` is the first: an Ubuntu container with our worker behind a WebSocket
bridge (``cua/``). Adding one means a folder like it and an entry here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .runtime import PythonRuntimeError

PROVIDERS = ("cua",)


async def connect(provider: str, url: str, token: str, *, signal: asyncio.Event | None = None, timeout_s: float = 20.0,
                  role: str | None = None) -> Any:
    """The provider's runtime for one run, ready to take cells; as a "viewer", one beside the run's, for the monitor."""
    if provider == "cua":
        from .cua.client import connect_remote

        return await connect_remote(url, token, signal=signal, timeout_s=timeout_s, role=role)
    raise PythonRuntimeError(f"Unknown remote desktop provider {provider!r}; one of: {', '.join(PROVIDERS)}.")
