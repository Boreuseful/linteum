"""ToolRuntime: the single subscriber to ``tool.call.requested`` (§22).

The runtime is the *only* component allowed to publish ``tool.call.*`` events
(§6 namespace table).  It sits between the pure agent (which only asks for a
tool by name) and the sandbox (which knows how to run it) and owns everything
that is stateful and impure about executing a tool:

* **accept / reject** — allowlist lookup, distinguishing ``UnknownTool`` from
  ``ToolUnavailable`` (§21);
* **acknowledgement** — ``tool.call.started`` so the UI stops waiting;
* **approval** — delegate ``requires_approval`` calls to
  :class:`~linteum_agent.runtime.tools.approval.ApprovalGate` without executing them;
* **concurrency + timeout** — per-tool semaphore (§21) and
  ``asyncio.timeout(spec.timeout)`` (Checklist #6);
* **idempotency** — consult / record the :class:`~linteum_agent.runtime.tools.results.ResultStore`
  by ``call_id`` before running a side effect (Checklist #8);
* **externalisation** — tool output over ``inline_limit`` becomes an ``art:``
  reference so ``messages`` never blows the ``INLINE_LIMIT`` (§22 / I15);
* **error-as-data** — every failure (P4) becomes ``tool.call.completed`` /
  ``tool.call.failed``; only ``asyncio.CancelledError`` is re-raised (I19).

Nothing here touches :class:`~linteum_agent.runtime.state.AgentState`: the runtime reacts to
events and publishes events, and the agent reduces them independently (P1/P3).
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Callable, Optional

from ...bus.artifact import NullArtifactStore
from ..errors import ErrorSource, error_info
from ...bus.scheduler.cancel import Cancelled, CancelToken
from ...bus.scheduler.pool import WorkerPool
from .errors import (
    E_BAD_ARGS,
    E_CANCELLED,
    E_DENIED,
    E_PLUGIN_UNLOADING,
    E_TIMEOUT,
    E_TOOL_ERROR,
    E_UNKNOWN_TOOL,
    ToolError,
    ToolUnavailable,
    UnknownTool,
    ValidationError,
)

#: Tool output larger than this is externalised to an artifact (§22, default 8 KB).
DEFAULT_INLINE_LIMIT = 8 * 1024
#: Dedicated pool size used only when the bus exposes no pool of its own.
DEFAULT_POOL_SIZE = 16

#: Stable ``location`` strings embedded in ``error_info`` (tool failures).
LOCATION_ON_CALL = "linteum_agent.runtime.tools.runtime.ToolRuntime.on_call"
LOCATION_EXECUTE = "linteum_agent.runtime.tools.runtime.ToolRuntime._execute"
LOCATION_DENY = "linteum_agent.runtime.tools.runtime.ToolRuntime.deny"


class ToolRuntime:
    """Executes tool calls and turns their outcomes into events (§22)."""

    def __init__(
        self,
        bus,
        registry,
        sandbox,
        results,
        approvals,
        *,
        pool=None,
        artifacts=None,
        inline_limit: int = DEFAULT_INLINE_LIMIT,
        auto_subscribe: bool = True,
        registry_for: Optional[Callable[[str], object]] = None,
        approval_policy: str = "ask",
    ) -> None:
        if approval_policy not in ("ask", "allow", "deny"):
            raise ValueError(
                f"approval_policy must be 'ask', 'allow' or 'deny', got {approval_policy!r}"
            )
        self.bus = bus
        self.reg = registry
        self.sandbox = sandbox
        self.results = results
        self.approvals = approvals
        #: Host policy for ``spec.requires_approval`` tools (§24):
        #: ``ask`` defers to the gate, ``allow`` executes immediately and
        #: ``deny`` pairs the call as ``tool.call.failed{E_DENIED}``.
        self.approval_policy = approval_policy
        #: Optional per-session registry selector (§ multi-tenant workspaces).
        #: ``None`` keeps the single-registry behaviour; a callable maps
        #: ``session_id`` -> :class:`ToolRegistry` (sync, or an awaitable of one
        #: so a host can lazily ``await register_many``).
        self.registry_for = registry_for
        # NB: don't use ``or`` here — stores may define ``__len__`` (falsy when empty).
        chosen = artifacts if artifacts is not None else getattr(bus, "artifacts", None)
        self.artifacts = chosen if chosen is not None else NullArtifactStore()
        self.inline_limit = inline_limit
        # Sharing the bus pool makes ``bus.wait_until_idle()`` cover tool work;
        # inject a private pool to isolate tool concurrency from event dispatch.
        self.pool = pool if pool is not None else getattr(bus, "pool", None) or WorkerPool(DEFAULT_POOL_SIZE)
        self.approvals.runtime = self

        self.tokens: dict[str, CancelToken] = {}
        self.inflight: dict[str, asyncio.Task] = {}
        self._sessions: dict[str, str] = {}

        if auto_subscribe:
            self.subscribe()

    # -- subscription -------------------------------------------------------
    def subscribe(self) -> None:
        self.bus.on("tool.call.requested", self.on_call)
        self.bus.on("tool.cancel.requested", self.on_cancel)
        self.bus.on("approval.granted", self.on_approval)
        self.bus.on("approval.denied", self.on_denial)

    # -- entry point (§22 ①–④) ---------------------------------------------
    async def on_call(self, ev) -> None:
        name = ev.payload.get("name")
        call_id = ev.payload.get("call_id")
        if not name or not call_id:
            return  # malformed intent — nothing to pair (I3 only binds well-formed calls)

        try:
            registry = await self._registry_for_session(ev.session_id)
        except Exception as exc:  # noqa: BLE001 - a bad factory must not hang the call
            await self._fail(ev, call_id, f"registry unavailable: {exc}", E_TOOL_ERROR, exc=exc)
            return

        try:
            spec = registry.spec(name)
        except UnknownTool as exc:
            await self._fail(ev, call_id, f"UnknownTool: {name}", E_UNKNOWN_TOOL, exc=exc)
            return
        except ToolUnavailable as exc:
            await self._fail(ev, call_id, f"tool unavailable: {name}", E_PLUGIN_UNLOADING, exc=exc)
            return

        # ② immediate acknowledgement so the UI knows the call was accepted
        await self.bus.publish(ev.child("tool.call.started", {
            "call_id": call_id, "name": name, "side_effect": spec.side_effect,
        }))

        # ③ approval-gated: suspend, do not execute
        if spec.requires_approval and self.approval_policy != "allow":
            if self.approval_policy == "deny":
                await self._fail(ev, call_id,
                                 f"approval required but policy is 'deny': {name}",
                                 E_DENIED)
                return
            await self.approvals.request(ev, spec)
            return

        # ④ fire-and-forget on the worker pool (non-blocking)
        await self._spawn(ev, spec, registry)

    async def resume_approved(self, ev) -> None:
        """Re-enter execution after a human granted the request (§24)."""
        name = ev.payload.get("name")
        try:
            registry = await self._registry_for_session(ev.session_id)
        except Exception as exc:  # noqa: BLE001 - pair the call even on a bad factory
            await self._fail(ev, ev.payload.get("call_id"), f"registry unavailable: {exc}",
                             E_TOOL_ERROR, exc=exc)
            return
        try:
            spec = registry.spec(name)
        except (UnknownTool, ToolUnavailable) as exc:
            await self._fail(ev, ev.payload.get("call_id"), f"tool unavailable: {name}",
                             E_PLUGIN_UNLOADING, exc=exc)
            return
        await self._spawn(ev, spec, registry)

    async def deny(self, ev, reason: str = "approval denied") -> None:
        """Resolve a rejected request as ``tool.call.failed{E_DENIED}`` (§24)."""
        info = error_info(E_DENIED, source=ErrorSource.TOOL.value, location=LOCATION_DENY,
                          message=reason, enters_history=True)
        await self._finish(ev, ev.payload.get("call_id"),
                           {"ok": False, "code": E_DENIED, "error": reason, "error_info": info.to_dict()})

    async def _spawn(self, ev, spec, registry=None) -> None:
        call_id = ev.payload["call_id"]
        token = CancelToken()
        self.tokens[call_id] = token
        self._sessions[call_id] = ev.session_id
        task = self.pool.spawn(self._execute(ev, spec, token, registry), session_id=ev.session_id)
        self.inflight[call_id] = task
        task.add_done_callback(lambda _t, cid=call_id: self.inflight.pop(cid, None))

    async def _registry_for_session(self, session_id: str):
        """Select the registry for ``session_id`` (``self.reg`` when unhooked).

        ``registry_for`` may return a registry directly or an awaitable of one
        (a host that lazily ``await register_many``); anything else falls back to
        the default registry so single-registry wiring is unchanged.
        """
        provider = self.registry_for
        if provider is None:
            return self.reg
        chosen = provider(session_id)
        if inspect.isawaitable(chosen):
            chosen = await chosen
        return chosen if chosen is not None else self.reg

    # -- execution (§22) ----------------------------------------------------
    async def _execute(self, ev, spec, token: CancelToken, registry=None) -> None:
        call_id = ev.payload["call_id"]
        reg = self.reg if registry is None else registry
        try:
            cached = await self.results.get(call_id)          # idempotency: check first
            if cached is not None:
                await self._finish(ev, call_id, dict(cached), dedup=True)
                return

            args = spec.validate(ev.payload.get("args") or {})  # schema check

            async def progress(payload: dict) -> None:
                await self.bus.publish(ev.child("tool.output.delta", {"call_id": call_id, **payload}))

            async with reg.semaphore(spec.name):              # per-tool concurrency gate
                async with asyncio.timeout(spec.timeout):    # hard timeout
                    out = await self.sandbox.run(spec, args, token, progress, call_id=call_id)
            result = {"ok": True, "output": self._normalise(out)}
        except asyncio.CancelledError:                        # I19 — never swallow
            try:
                await self._finish(ev, call_id, self._error(
                    E_CANCELLED, "cancelled", location=LOCATION_EXECUTE, enters_history=False))
            except Exception:  # noqa: BLE001 - best effort while being cancelled
                pass
            raise
        except Cancelled as exc:                              # cooperative token cancel
            result = self._error(E_CANCELLED, "cancelled", exc=exc,
                                 location=LOCATION_EXECUTE, enters_history=False)
        except TimeoutError as exc:
            result = self._error(E_TIMEOUT, f"timeout after {spec.timeout}s", exc=exc,
                                 location=LOCATION_EXECUTE, retryable=True)
        except ValidationError as exc:
            result = {"ok": False, **exc.as_payload(location=LOCATION_EXECUTE)}
        except ToolError as exc:
            result = {"ok": False, **exc.as_payload(location=LOCATION_EXECUTE)}
        except Exception as exc:  # noqa: BLE001 - errors are data (P4)
            result = self._error(E_TOOL_ERROR, f"{type(exc).__name__}: {exc}", exc=exc,
                                 location=LOCATION_EXECUTE, retryable=True)
        await self._finish(ev, call_id, result)

    async def _finish(self, ev, call_id, result: dict, dedup: bool = False) -> None:
        """Externalise oversized output, record the result, publish the outcome."""
        self.tokens.pop(call_id, None)
        self._sessions.pop(call_id, None)

        ok = bool(result.get("ok"))
        if ok:
            output = result.get("output")
            text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
            if len(text.encode("utf-8")) > self.inline_limit and getattr(self.artifacts, "available", False):
                art = await self.artifacts.put(text.encode("utf-8"), mime="text/plain", session_id=ev.session_id)
                result = {
                    **result,
                    "output": f"[tool output externalized: {art.ref}]",
                    "output_ref": art.ref,
                    "output_tokens": self._estimate_tokens(text),
                }
            await self.results.put(call_id, result)           # idempotency: record after

        etype = "tool.call.completed" if ok else "tool.call.failed"
        await self.bus.publish(ev.child(etype, {
            "call_id": call_id, "name": ev.payload.get("name"), "dedup": dedup, **result,
        }))

    async def _fail(self, ev, call_id, message: str, code: str, *, exc=None) -> None:
        info = error_info(code, source=ErrorSource.TOOL.value, location=LOCATION_ON_CALL,
                          message=message, exc=exc, retryable=(code == E_PLUGIN_UNLOADING))
        await self._finish(ev, call_id,
                           {"ok": False, "code": code, "error": message, "error_info": info.to_dict()})

    # -- cancellation (§16.3) ----------------------------------------------
    async def on_cancel(self, ev) -> None:
        # Invalidate approval first (synchronously): a decision racing with the
        # interrupt must never reach ``resume_approved`` and execute the tool.
        call_id = ev.payload.get("call_id")
        if call_id:
            self.approvals.cancel(call_id=call_id)
            await self._cancel_call(call_id)
            return
        session_id = ev.payload.get("session_id") or ev.session_id
        self.approvals.cancel(session_id=session_id)
        for cid in [c for c, s in self._sessions.items() if s == session_id]:
            await self._cancel_call(cid)

    async def _cancel_call(self, call_id: str) -> None:
        token = self.tokens.get(call_id)
        if token is not None:
            token.cancel()                                  # stop cooperative workers
        task = self.inflight.get(call_id)
        if task is not None and not task.done():
            task.cancel()                                   # interrupt awaits (I19 re-raises)
        await self.sandbox.kill(call_id)                    # real teardown (p.kill())

    # -- approval (§24) -----------------------------------------------------
    async def on_approval(self, ev) -> None:
        await self.approvals.on_granted(ev)

    async def on_denial(self, ev) -> None:
        await self.approvals.on_denied(ev)

    # -- helpers ------------------------------------------------------------
    def _error(self, code: str, message: str, *, exc=None, location: Optional[str] = None,
               retryable: bool = False, enters_history: bool = True) -> dict:
        """Build a ``tool.call.failed`` result with a structured ``error_info``.

        This is what makes a failure *unambiguous*: the payload keeps the shared
        ``code`` (e.g. ``E_TIMEOUT``) but ``error_info.source == "tool"`` says the
        tool layer produced it, and ``detail``/``exc_type``/``location`` say what
        and where.
        """
        info = error_info(code, source=ErrorSource.TOOL.value,
                          location=location or LOCATION_EXECUTE, message=message,
                          exc=exc, retryable=retryable, enters_history=enters_history)
        return {"ok": False, "code": code, "error": message, "error_info": info.to_dict()}

    @staticmethod
    def _normalise(output):
        """Coerce a tool result to something the bus can serialise (P2)."""
        if output is None or isinstance(output, (str, int, float, bool)):
            return output
        try:
            json.dumps(output)
            return output
        except (TypeError, ValueError):
            return str(output)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        from ..llm.canonical import estimate_tokens  # lazy: avoid an import cycle
        return estimate_tokens(text)

