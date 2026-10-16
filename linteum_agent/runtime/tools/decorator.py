"""The ``@tool`` decorator: turn a plain function into a ``ToolSpec`` carrier (§21).

A tool is *just a function*.  It does not know about :class:`~linteum_agent.bus.event.Event`,
never calls ``publish`` and is never told its ``session_id`` (P3 / Checklist #3).
The decorator only attaches a frozen :class:`~linteum_agent.runtime.tools.spec.ToolSpec` as
``fn.__tool_spec__`` so the assembly-time registry can pick it up; the callable
itself is returned unchanged and stays a normal, testable function.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from .spec import ToolSpec

#: Side-effect levels that force human approval unless overridden (§21 / §24).
APPROVAL_SIDE_EFFECTS: frozenset = frozenset({"write", "dangerous"})


def tool(
    description: str = "",
    params: Optional[dict] = None,
    *,
    required: Sequence[str] = (),
    side_effect: str = "read",
    timeout: float = 30.0,
    idempotent: bool = True,
    concurrency: int = 4,
    backend: str = "inproc",
    requires_approval: Optional[bool] = None,
    name: Optional[str] = None,
    plugin_id: Optional[str] = None,
    entrypoint: Optional[str] = None,
) -> Callable[[Callable], Callable]:
    """Decorator factory attaching a :class:`ToolSpec` to ``fn``.

    ``requires_approval`` defaults to ``True`` for ``write`` / ``dangerous``
    tools (the safe default) and can be overridden explicitly (§21).
    """

    def decorate(fn: Callable) -> Callable:
        spec = ToolSpec(
            name=name or fn.__name__,
            fn=fn,
            description=description or (fn.__doc__ or "").strip(),
            params=dict(params or {}),
            required_params=tuple(required),
            side_effect=side_effect,
            timeout=timeout,
            idempotent=idempotent,
            concurrency=concurrency,
            backend=backend,
            requires_approval=(
                requires_approval
                if requires_approval is not None
                else side_effect in APPROVAL_SIDE_EFFECTS
            ),
            plugin_id=plugin_id,
            entrypoint=entrypoint,
        )
        fn.__tool_spec__ = spec  # type: ignore[attr-defined]
        return fn

    return decorate


def spec_of(fn: Callable) -> ToolSpec:
    """Return the spec attached by :func:`tool` (or raise a clear error)."""
    spec = getattr(fn, "__tool_spec__", None)
    if spec is None:
        raise AttributeError(f"{fn!r} is not decorated with @tool")
    return spec
