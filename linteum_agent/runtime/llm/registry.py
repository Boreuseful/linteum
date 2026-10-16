"""Adapter registry and route definitions (§43).

``AdapterRegistry`` maps a provider name to its adapter; an alias maps to an
ordered fallback :class:`Route` of ``(provider, model)`` candidates.  Route
resolution (pinning, capability filtering) lives in :mod:`linteum_agent.runtime.llm.router`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .canonical import Capabilities
from .errors import DuplicateAdapter, NoAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict = {}
        self._aliases: dict = {}

    # -- adapters -----------------------------------------------------------
    def register(self, adapter) -> None:
        if adapter.provider in self._adapters:
            raise DuplicateAdapter(adapter.provider)
        self._adapters[adapter.provider] = adapter

    def get(self, provider: str):
        try:
            return self._adapters[provider]
        except KeyError as exc:
            raise NoAdapter(f"no adapter registered for provider {provider!r}") from exc

    def has(self, provider: str) -> bool:
        return provider in self._adapters

    def providers(self) -> tuple:
        return tuple(self._adapters.keys())

    # -- aliases ------------------------------------------------------------
    def alias(
        self,
        name: str,
        chain,
        *,
        pin_per_session: bool = True,
        require: frozenset = frozenset(),
    ) -> "Route":
        route = Route(name, tuple(tuple(c) for c in chain), pin_per_session, require)
        self._aliases[name] = route
        return route

    def route(self, alias: str) -> Optional["Route"]:
        return self._aliases.get(alias)


@dataclass(frozen=True)
class Route:
    alias: str
    chain: tuple                     # ((provider, model), ...) preferred first
    pin_per_session: bool = True     # lock provider/model within a session (I25)
    require: frozenset = field(default_factory=frozenset)  # required capabilities (§43)


#: Which Capabilities attribute each requirement name maps to (§43).
_REQUIREMENTS = {
    "tools": "tools",
    "reasoning": "reasoning",
    "json_mode": "json_mode",
    "strict_schema": "strict_schema",
}


def satisfies(cap: Capabilities, require: frozenset) -> bool:
    for name in require:
        attr = _REQUIREMENTS.get(name, name)
        if not getattr(cap, attr, False):
            return False
    return True
