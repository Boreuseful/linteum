"""ToolRegistry: the assembly-time allowlist of available tools (§21).

The registry is the *only* place that holds ``spec.fn``.  At run time the LLM
sees merely ``schemas()`` (JSON), the runtime resolves a call with ``spec(name)``
and concurrency is gated per tool by ``semaphore(name)``.  Two properties matter:

* **atomic batch registration** — ``register_many`` validates the whole batch
  before mutating anything, so a plugin whose ``setup()`` raises can never leave
  half its tools installed (Checklist #23);
* **draining ≠ unknown** — a tool whose plugin is being unloaded raises
  :class:`ToolUnavailable` (retryable), never :class:`UnknownTool` (the LLM
  hallucinated a name), because the two need opposite model reactions (§21).
"""

from __future__ import annotations

import asyncio
from typing import Iterable

from .errors import DuplicateTool, ToolUnavailable, UnknownTool
from .spec import ToolSpec


class ToolRegistry:
    """In-process name → :class:`ToolSpec` table with per-tool semaphores.

    ``reserved`` names belong to a host capability that is not a real tool (the
    subagent ``spawn_subagent`` routing name).  Registering one is a conflict:
    the model must never see two schemas with the same name.
    """

    def __init__(self, *, reserved: Iterable[str] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._sem: dict[str, asyncio.Semaphore] = {}
        self._draining: set[str] = set()
        self._reserved: set[str] = set(reserved)
        self._lock = asyncio.Lock()

    def reserve(self, name: str) -> None:
        """Mark ``name`` as host-reserved (assembly-time assertion, not a spec)."""
        self._reserved.add(name)

    # -- mutation (atomic) --------------------------------------------------
    async def register_many(self, specs: Iterable[ToolSpec]) -> None:
        """Install a batch of specs atomically; duplicates abort the whole batch."""
        batch = list(specs)
        async with self._lock:
            seen: set[str] = set()
            for spec in batch:
                if spec.name in self._specs or spec.name in seen or spec.name in self._reserved:
                    raise DuplicateTool(spec.name)
                seen.add(spec.name)
            for spec in batch:
                self._specs[spec.name] = spec
                self._sem[spec.name] = asyncio.Semaphore(spec.concurrency)
                self._draining.discard(spec.name)

    async def register(self, spec: ToolSpec) -> None:
        await self.register_many([spec])

    async def unregister_many(self, names: Iterable[str]) -> list[ToolSpec]:
        """Remove a batch of tools; returns the specs that were actually removed."""
        removed: list[ToolSpec] = []
        async with self._lock:
            for name in names:
                spec = self._specs.pop(name, None)
                if spec is not None:
                    removed.append(spec)
                self._sem.pop(name, None)
                self._draining.discard(name)
        return removed

    def mark_draining(self, names: Iterable[str]) -> None:
        """Flag tools as temporarily unavailable (their plugin is quiescing)."""
        self._draining |= set(names)

    # -- lookup -------------------------------------------------------------
    def spec(self, name: str) -> ToolSpec:
        """Resolve a tool or raise the *right* error (Unavailable vs Unknown)."""
        if name in self._draining:
            raise ToolUnavailable(name)
        try:
            return self._specs[name]
        except KeyError as exc:
            raise UnknownTool(name) from exc

    def has(self, name: str) -> bool:
        return name in self._specs and name not in self._draining

    def schemas(self) -> list[dict]:
        """Function-calling schemas for the live (non-draining) tools."""
        return [s.json_schema() for name, s in self._specs.items() if name not in self._draining]

    def semaphore(self, name: str) -> asyncio.Semaphore:
        """The per-tool concurrency gate (Checklist #6)."""
        return self._sem[name]

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def draining(self) -> tuple[str, ...]:
        return tuple(sorted(self._draining))

    def __len__(self) -> int:
        return len(self._specs)
