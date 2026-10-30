"""Import, ``setup`` execution and staged-tool validation (§5.3 / §5.4 / §8.4).

Loading is two-phase: ``setup(ctx)`` only stages :class:`ToolSpec` values, then
:func:`validate_staged` checks every declaration against the staging area and
returns the final specs.  The host commits them atomically, so a plugin that
fails halfway never leaves a partial tool table.

Synchronous ``import``/``setup`` run on ``asyncio.to_thread`` so a blocking
plugin cannot stall the event loop.  Python cannot kill a thread; a timed-out
synchronous loader is *abandoned* and counted.  Once
:data:`MAX_ABANDONED_LOADERS` loaders are still running, further loads are
refused instead of piling up unbounded threads.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import sys
import threading
from dataclasses import replace
from typing import Any, Callable, Optional

from ..tools.spec import ToolSpec
from .context import Staging
from .errors import PluginLoadError, PluginTimeout, PluginToolMissing, PluginToolUndeclared
from .model import PluginManifest

#: Refuse new loads while this many synchronous loaders are still running.
MAX_ABANDONED_LOADERS = 8

_abandoned = 0
_abandoned_lock = threading.Lock()
#: Serialise the temporary ``sys.path`` mutation across concurrent loads (§5.4).
_import_lock = threading.Lock()


def abandoned_loaders() -> int:
    """Number of synchronous loaders whose timeout has already been reported."""
    with _abandoned_lock:
        return _abandoned


def _mark_abandoned(task: "asyncio.Task") -> None:
    global _abandoned
    with _abandoned_lock:
        _abandoned += 1

    def _done(done: "asyncio.Task") -> None:
        global _abandoned
        with _abandoned_lock:
            _abandoned = max(0, _abandoned - 1)
        with contextlib.suppress(BaseException):
            done.exception()  # retrieve so the loop does not warn

    task.add_done_callback(_done)


async def _call_sync(fn: Callable[[], Any], timeout: float, *, what: str) -> Any:
    """Run ``fn`` in a worker thread, bounded by ``timeout`` seconds.

    A timeout does not cancel the thread; it is abandoned and tracked so the
    host can refuse to accumulate unbounded blocked loaders.
    """
    with _abandoned_lock:
        if _abandoned >= MAX_ABANDONED_LOADERS:
            raise PluginTimeout(
                f"{MAX_ABANDONED_LOADERS} abandoned plugin loaders are still running",
                key="load_timeout_ms",
                hint="restart the host after fixing the blocking plugin",
            )
    task = asyncio.create_task(asyncio.to_thread(fn))
    done, pending = await asyncio.wait({task}, timeout=timeout)
    if pending:
        _mark_abandoned(task)
        raise PluginTimeout(
            f"plugin {what} timed out after {timeout:.1f}s",
            key="load_timeout_ms",
            context={"phase": what, "timeout_ms": int(timeout * 1000)},
        )
    return task.result()


def _import_module(manifest: PluginManifest):
    """Import the entry module with the plugin root temporarily on ``sys.path``."""
    assert manifest.entry is not None
    root = str(manifest.root)
    with _import_lock:
        inserted = root not in sys.path
        if inserted:
            sys.path.insert(0, root)
        try:
            return importlib.import_module(manifest.entry.path.stem)
        finally:
            if inserted:
                with contextlib.suppress(ValueError):
                    sys.path.remove(root)


def _resolve_setup(module: Any, qualname: str) -> Callable:
    target: Any = module
    try:
        for part in qualname.split("."):
            target = getattr(target, part)
    except AttributeError as exc:
        raise PluginLoadError(
            f"entry callable {qualname!r} was not found",
            key="entry", detail=str(exc), exc_type=type(exc).__name__,
        ) from exc
    if not callable(target):
        raise PluginLoadError(
            f"entry {qualname!r} is not callable", key="entry",
        )
    return target


async def import_and_setup(
    manifest: PluginManifest,
    ctx,
    *,
    timeout_ms: int,
    log: Optional[Callable[[dict], None]] = None,
) -> Any:
    """Import ``entry`` and call ``setup(ctx)`` within one time budget.

    Content-only plugins (skills/modes) have no entry and are never imported
    (D3).  The returned module is kept only so callables stay alive; nothing
    about it is serialised.
    """
    if manifest.entry is None:
        return None
    timeout = max(0.001, timeout_ms / 1000.0)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    module = await _call_sync(
        lambda: _import_module(manifest),
        max(0.001, deadline - loop.time()),
        what="import",
    )
    setup = _resolve_setup(module, manifest.entry.qualname)
    remaining = max(0.001, deadline - loop.time())
    if inspect.iscoroutinefunction(setup):
        try:
            await asyncio.wait_for(setup(ctx), remaining)
        except TimeoutError as exc:
            raise PluginTimeout(
                f"plugin setup timed out after {timeout:.1f}s",
                key="load_timeout_ms",
                context={"phase": "setup"},
                detail=str(exc), exc_type=type(exc).__name__,
            ) from exc
    else:
        await _call_sync(lambda: setup(ctx), remaining, what="setup")
    return module


def validate_staged(
    staging: Staging, manifest: PluginManifest,
) -> tuple[tuple[ToolSpec, ...], tuple[str, ...]]:
    """Match staged specs to declarations and take the stricter security value."""
    staged, _callbacks = staging.snapshot()
    provided: dict[str, ToolSpec] = {}
    for spec in staged:
        if spec.name in provided:
            raise PluginLoadError(
                f"tool {spec.name!r} was staged twice", key="tools",
                context={"plugin_id": manifest.id, "tool": spec.name},
            )
        provided[spec.name] = spec

    declared = {item.name: item for item in manifest.tools}
    undeclared = sorted(set(provided) - set(declared))
    if undeclared:
        raise PluginToolUndeclared(
            f"tool(s) not declared in plugin.yaml: {', '.join(undeclared)}",
            key="capabilities.tools",
            context={"plugin_id": manifest.id, "tools": undeclared},
        )
    missing = [item.name for item in manifest.tools if item.name not in provided]
    if missing:
        raise PluginToolMissing(
            f"declared tool(s) were not provided by setup: {', '.join(missing)}",
            key="capabilities.tools",
            context={"plugin_id": manifest.id, "tools": missing},
        )

    final: list[ToolSpec] = []
    warnings: list[str] = []
    for declaration in manifest.tools:
        spec = provided[declaration.name]
        _check_spec(spec, declaration, manifest)
        if not spec.description:
            warnings.append(
                f"plugin.tool_description_empty: {manifest.id}/{spec.name}"
            )
        requires_approval = (
            spec.requires_approval
            or declaration.requires_approval
            or declaration.side_effect in ("write", "dangerous")
        )
        final.append(replace(
            spec,
            requires_approval=requires_approval,
            plugin_id=manifest.id,
            entrypoint=None,
        ))
    return tuple(final), tuple(warnings)


def _spec_error(manifest: PluginManifest, spec: ToolSpec, message: str):
    return PluginLoadError(
        message,
        key="capabilities.tools",
        context={"plugin_id": manifest.id, "tool": spec.name},
    )


def _check_spec(spec: ToolSpec, declaration, manifest: PluginManifest) -> None:
    """Enforce §5.3: declarations are the security floor, never widened."""
    if not callable(spec.fn):
        raise _spec_error(manifest, spec, f"tool {spec.name!r} fn is not callable")
    if spec.side_effect != declaration.side_effect:
        raise _spec_error(
            manifest, spec,
            f"tool {spec.name!r} side_effect {spec.side_effect!r} does not match "
            f"the declared {declaration.side_effect!r}",
        )
    if spec.timeout > declaration.timeout:
        raise _spec_error(
            manifest, spec,
            f"tool {spec.name!r} timeout {spec.timeout} exceeds the declared "
            f"{declaration.timeout}",
        )
    if spec.concurrency > declaration.concurrency:
        raise _spec_error(
            manifest, spec,
            f"tool {spec.name!r} concurrency {spec.concurrency} exceeds the "
            f"declared {declaration.concurrency}",
        )
    if bool(spec.idempotent) != bool(declaration.idempotent):
        raise _spec_error(
            manifest, spec,
            f"tool {spec.name!r} idempotent does not match the declaration",
        )
    if spec.backend != declaration.backend:
        raise _spec_error(
            manifest, spec,
            f"tool {spec.name!r} backend {spec.backend!r} does not match the "
            f"declared {declaration.backend!r}",
        )
    if spec.entrypoint is not None:
        raise _spec_error(
            manifest, spec,
            f"tool {spec.name!r} must not set entrypoint in v0.1",
        )
    if not isinstance(spec.params, dict):
        raise _spec_error(manifest, spec, f"tool {spec.name!r} params must be a dict")
    if not set(spec.required_params or ()) <= set(spec.params):
        raise _spec_error(
            manifest, spec,
            f"tool {spec.name!r} required_params must be a subset of params",
        )


async def run_cleanup(
    callbacks: tuple[Callable[[], Any], ...],
    *,
    timeout_ms: int,
    log: Optional[Callable[[dict], None]] = None,
) -> None:
    """Call unload callbacks in reverse registration order, best effort.

    The whole sequence shares one ``unload_timeout_ms`` budget.  Exceptions are
    logged and never abort the remaining callbacks; ``CancelledError`` is
    re-raised (I19).
    """
    if not callbacks:
        return

    def _log(message: str, **fields: object) -> None:
        if log is None:
            return
        with contextlib.suppress(Exception):
            log({
                "kind": "plugin", "level": "warning", "msg": message,
                **fields,
            })

    async def _run_all() -> None:
        for callback in reversed(callbacks):
            try:
                if inspect.iscoroutinefunction(callback):
                    await callback()
                else:
                    result = await asyncio.to_thread(callback)
                    if inspect.isawaitable(result):
                        await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one callback must not abort unload
                _log(f"plugin unload callback failed: {type(exc).__name__}: {exc}")

    try:
        await asyncio.wait_for(_run_all(), max(0.001, timeout_ms / 1000.0))
    except TimeoutError:
        _log(f"plugin unload callbacks timed out after {timeout_ms} ms")


__all__ = [
    "MAX_ABANDONED_LOADERS",
    "abandoned_loaders",
    "import_and_setup",
    "run_cleanup",
    "validate_staged",
]
