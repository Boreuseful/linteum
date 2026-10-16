"""Offline tools and helpers for the tool-system tests and examples (§50).

Offline sample tools (the LLM testing fakes are not part of this migration): a batch of tiny, deterministic tools covering
the interesting execution paths (sync, async, streaming, failing, slow, and an
importable module-level function for the L2 subprocess backend), plus an
in-memory MCP server so the bridge can be exercised with no network.

Because :func:`subproc_echo` lives at module scope it is importable by qualified
name in a child process — which is exactly what the subprocess backend needs.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Iterable, Optional

from .decorator import tool
from .mcp_bridge import McpFrame
from .registry import ToolRegistry


@tool("Return the current weather for a city.", {"city": {"type": "string"}},
      required=["city"], side_effect="read")
def get_weather(city: str) -> str:
    return f"sunny in {city}"


@tool("Echo the given text back.", {"text": {"type": "string"}},
      required=["text"], side_effect="pure")
def echo(text: str) -> str:
    return text


@tool("Always raise, to exercise the failure path.", {}, side_effect="read")
def boom() -> str:
    raise RuntimeError("boom")


@tool("Sleep for `seconds` (used to exercise timeouts).",
      {"seconds": {"type": "number"}}, required=["seconds"], side_effect="read", timeout=0.05)
async def slow(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "finally done"


@tool("Stream `n` chunks via progress, then return a final answer.",
      {"n": {"type": "integer"}}, required=["n"], side_effect="read")
async def streamy(n: int, progress) -> str:
    for index in range(n):
        await progress({"chunk": f"{index} "})
    return "streamed"


@tool("Add two integers (module-level, importable out-of-process).",
      {"a": {"type": "integer"}, "b": {"type": "integer"}},
      required=["a", "b"], side_effect="pure", backend="subproc")
def subproc_add(a: int, b: int) -> int:
    return a + b


@tool("Write to a file (side-effect: requires approval).",
      {"path": {"type": "string"}, "text": {"type": "string"}}, required=["path", "text"],
      side_effect="write")
def write_note(path: str, text: str) -> str:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


#: Convenience list of every sample spec (order stable for deterministic tests).
def default_specs() -> list:
    return [
        get_weather.__tool_spec__,
        echo.__tool_spec__,
        boom.__tool_spec__,
        slow.__tool_spec__,
        streamy.__tool_spec__,
        subproc_add.__tool_spec__,
        write_note.__tool_spec__,
    ]


async def build_registry(specs: Optional[Iterable] = None) -> ToolRegistry:
    """A registry pre-loaded with the sample tools (or a supplied batch)."""
    registry = ToolRegistry()
    await registry.register_many(list(specs) if specs is not None else default_specs())
    return registry


class InMemoryMcpServer:
    """A scripted MCP server for offline tests of :class:`McpBridge`."""

    def __init__(self, chunks: Iterable[str] = (), result=None,
                 error: Optional[str] = None) -> None:
        self._chunks = list(chunks)
        self._result = result
        self._error = error

    async def stream_call(self, name: str, args: dict) -> AsyncIterator[McpFrame]:
        for chunk in self._chunks:
            yield McpFrame("partial", {"chunk": chunk})
        if self._error is not None:
            yield McpFrame("error", {"code": "E_TOOL_ERROR", "error": self._error})
        else:
            yield McpFrame("final", {"output": self._result, "name": name})
