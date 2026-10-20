"""Model routing: alias -> ordered candidates, with session pinning (§43, I25).

``AdapterRegistry`` (see :mod:`linteum_agent.runtime.llm.registry`) holds the adapters and the
alias table; this module turns a request into an **ordered candidate list** and
enforces the session lock.

**I25** — a session locks its provider/model on the first successful call.
Switching mid-session would break ``tool_call_id`` pairing (I11), void the prompt
cache and possibly flip the reasoning policy, so it is only ever explicit, via
:meth:`LlmRouter.transplant`.
"""

from __future__ import annotations

from typing import Optional

from .canonical import LlmRequest
from .errors import NoAdapter
from .registry import AdapterRegistry, Route, satisfies


class LlmRouter:
    def __init__(self, registry: AdapterRegistry, *, breaker=None) -> None:
        self.registry = registry
        self.breaker = breaker
        self._pinned: dict = {}

    # -- routing ------------------------------------------------------------
    def route_for(self, alias: str) -> Route:
        route = self.registry.route(alias)
        if route is None:
            raise NoAdapter(f"no route configured for alias {alias!r}")
        return route

    def candidates(self, req: LlmRequest, session_id: str) -> list:
        """Ordered ``[(provider, model, adapter), ...]`` respecting pin + capabilities."""
        route = self.route_for(req.model)
        pinned = self._pinned.get(session_id)
        if pinned is not None:
            if route.pin_per_session:
                chain = [pinned]
            else:
                chain = [pinned] + [c for c in route.chain if c != pinned]
        else:
            chain = list(route.chain)

        out: list = []
        for provider, model in chain:
            if not self.registry.has(provider):
                continue
            adapter = self.registry.get(provider)
            capability = adapter.capabilities(model)
            if req.tools and not capability.tools:
                continue  # e.g. the model (or a user capability_map) declares tools=False
            if not satisfies(capability, route.require):
                continue
            if self.breaker is not None and not self.breaker.allow((provider, model)):
                continue
            out.append((provider, model, adapter))
        return out

    # -- pinning (I25) ------------------------------------------------------
    def pinned(self, session_id: str) -> Optional[tuple]:
        return self._pinned.get(session_id)

    def pin(self, session_id: str, provider: str, model: str) -> None:
        self._pinned.setdefault(session_id, (provider, model))

    def transplant(self, session_id: str, provider: str, model: str) -> Optional[tuple]:
        """The *only* sanctioned way to switch provider/model mid-session (I25)."""
        previous = self._pinned.get(session_id)
        self._pinned[session_id] = (provider, model)
        return previous
