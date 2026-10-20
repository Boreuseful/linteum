"""McpBridge: proxy remote MCP tools onto the bus (§24, L4).

This is the one place where a tool *literally* travels over the bus: a channel
plugin that speaks MCP streams ``partial`` / ``final`` / ``error`` frames, which
map one-to-one onto the tool events the rest of the system already understands:

======================  ==========================
 MCP frame               bus event
======================  ==========================
 ``partial``             ``tool.output.delta``
 ``final``               ``tool.call.completed``
 ``error``               ``tool.call.failed``
======================  ==========================

Because the mapping is total, swapping a local function for a remote MCP server
is invisible above this module (G7 — 位置无关).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, runtime_checkable

from .errors import E_TOOL_ERROR, E_UNKNOWN_TOOL

#: MCP frame kind → tool event type.
KIND_TO_EVENT: dict = {
    "partial": "tool.output.delta",
    "final": "tool.call.completed",
    "error": "tool.call.failed",
}


@dataclass(frozen=True)
class McpFrame:
    """One frame streamed by an MCP server."""

    kind: str
    data: dict = field(default_factory=dict)


@runtime_checkable
class McpServer(Protocol):
    """What the bridge needs from a server: a streaming call."""

    async def stream_call(self, name: str, args: dict) -> AsyncIterator[McpFrame]: ...


class McpRouter:
    """Route a tool name to an MCP server by prefix (``mcp://search:9000``)."""

    def __init__(self) -> None:
        self._routes: list[tuple[str, McpServer]] = []

    def mount(self, prefix: str, server: McpServer) -> None:
        self._routes.append((prefix, server))

    def route(self, name: str) -> McpServer:
        for prefix, server in reversed(self._routes):
            if name.startswith(prefix):
                return server
        if len(self._routes) == 1:
            return self._routes[0][1]  # a single mounted server serves everything
        raise KeyError(name)


class McpBridge:
    """Subscribe ``tool.call.requested`` and stream the server's frames."""

    def __init__(self, bus, servers: McpRouter, *, auto_subscribe: bool = True) -> None:
        self.bus = bus
        self.servers = servers
        if auto_subscribe:
            self.bus.on("tool.call.requested", self.on_call)

    async def on_call(self, ev) -> None:
        name = ev.payload.get("name")
        call_id = ev.payload.get("call_id")
        try:
            server = self.servers.route(name)
        except KeyError:
            await self.bus.publish(ev.child("tool.call.failed", {
                "call_id": call_id, "name": name,
                "code": E_UNKNOWN_TOOL, "error": f"no MCP server for {name!r}",
            }))
            return

        await self.bus.publish(ev.child("tool.call.started", {"call_id": call_id, "name": name}))
        try:
            async for frame in server.stream_call(name, ev.payload.get("args") or {}):
                etype = KIND_TO_EVENT.get(frame.kind)
                if etype is None:
                    continue
                payload = {"call_id": call_id, "name": name, **frame.data}
                if etype == "tool.call.completed":
                    payload.setdefault("ok", True)
                await self.bus.publish(ev.child(etype, payload))
                if etype != "tool.output.delta":
                    return  # final/error terminates the call
            await self.bus.publish(ev.child("tool.call.failed", {
                "call_id": call_id, "name": name,
                "code": E_TOOL_ERROR, "error": "MCP stream ended without a final frame",
            }))
        except asyncio.CancelledError:  # I19
            raise
        except Exception as exc:  # noqa: BLE001 - errors are data (P4)
            await self.bus.publish(ev.child("tool.call.failed", {
                "call_id": call_id, "name": name,
                "code": E_TOOL_ERROR, "error": repr(exc),
            }))
