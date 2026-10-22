"""RCU subscription table with match-priority ordering (§10).

Readers are lock-free and writers copy-on-write.  Because the whole framework
runs on a single asyncio event loop, the only rule the router must respect is
*"no ``await`` inside a mutation"* — with that, replacing the snapshot reference
is atomic with respect to dispatch, and a handler that (un)subscribes while
events are being delivered simply produces the next generation (``gen``).

Match priority (§9): **exact > prefix > glob > ``*``**.  ``*`` is handled
separately as the ``on_any`` observation bypass so an observer always runs, and
never accidentally shadows a targeted handler.
"""

from __future__ import annotations

import fnmatch
from typing import Callable

Handler = Callable[..., object]
Unsubscribe = Callable[[], None]

ANY = "*"


def _specificity(pattern: str) -> tuple:
    """Sort key: more dots first, then longer, then lexicographic (stable)."""
    return (-pattern.count("."), -len(pattern), pattern)


class Router:
    """Immutable-snapshot routing table.

    ``_table`` / ``_exact`` / ``_wild`` / ``_any`` are all swapped as whole
    objects, so a dispatch that captured a reference never sees a partial state.
    """

    __slots__ = ("_table", "_exact", "_wild", "_any", "gen")

    def __init__(self) -> None:
        self._table: dict[str, tuple[Handler, ...]] = {}
        self._exact: dict[str, tuple[Handler, ...]] = {}
        self._wild: tuple[tuple[str, tuple[Handler, ...]], ...] = ()
        self._any: tuple[Handler, ...] = ()
        self.gen = 0

    # -- mutation (no await allowed here) -----------------------------------
    def add(self, pattern: str, handler: Handler) -> Unsubscribe:
        """Register ``handler`` for ``pattern``; returns an idempotent remover."""
        new = {**self._table, pattern: (*self._table.get(pattern, ()), handler)}
        self._rebuild(new)
        self.gen += 1

        def unsubscribe() -> None:
            self.remove(pattern, handler)

        return unsubscribe

    def remove(self, pattern: str, handler: Handler) -> None:
        handlers = self._table.get(pattern)
        if not handlers or handler not in handlers:
            return
        remaining = tuple(h for h in handlers if h is not handler)
        new = dict(self._table)
        if remaining:
            new[pattern] = remaining
        else:
            del new[pattern]
        self._rebuild(new)
        self.gen += 1

    def _rebuild(self, new_table: dict[str, tuple[Handler, ...]]) -> None:
        self._table = new_table  # atomic reference swap
        self._exact = {p: hs for p, hs in new_table.items() if ANY not in p}
        wilds = [(p, hs) for p, hs in new_table.items() if ANY in p and p != ANY]
        wilds.sort(key=lambda kv: _specificity(kv[0]))
        self._wild = tuple(wilds)
        self._any = tuple(new_table.get(ANY, ()))

    # -- matching -----------------------------------------------------------
    def match(self, etype: str) -> tuple[Handler, ...]:
        """All handlers for ``etype``: on_any bypass, then exact, then wilds."""
        matched: list[Handler] = list(self._any)
        exact = self._exact.get(etype)
        if exact:
            matched.extend(exact)
        for pattern, handlers in self._wild:
            if fnmatch.fnmatchcase(etype, pattern):
                matched.extend(handlers)
        return tuple(matched)

    @property
    def any_handlers(self) -> tuple[Handler, ...]:
        return self._any

    # -- introspection ------------------------------------------------------
    def patterns(self) -> tuple[str, ...]:
        return tuple(self._table.keys())

    def snapshot(self) -> dict[str, tuple[Handler, ...]]:
        """Return the current table (used by tests and, later, tracers)."""
        return dict(self._table)

    def __len__(self) -> int:
        return sum(len(hs) for hs in self._table.values())
