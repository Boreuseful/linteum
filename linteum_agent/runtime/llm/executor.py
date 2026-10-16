"""LLMExecutor: the single subscriber to ``llm.request`` (§44), SDK-backed.

Responsibilities (everything the adapter may not do — I21):

* route + capability pre-check + **session pinning** (I25);
* clamp to capabilities, surface dropped params via `llm.params.dropped` (§38.4);
* emit ``llm.started`` with a redacted, serialisable preview of the SDK kwargs;
* drive the adapter stream, turning canonical chunks into ``llm.token.delta`` /
  ``llm.reasoning.delta`` events (delta coalescing is the bus's job, §13.1);
* on success emit ``llm.completed`` carrying every replay-relevant field;
* classify errors, retry/fallback per §41.3, record the breaker, and always turn
  failures into ``llm.failed`` events (P4) — never leak exceptions, never swallow
  ``CancelledError`` (I19).

The adapter now owns the provider SDK client; there is no separate transport.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from ...bus.event import Event
from ..errors import ErrorSource, error_info
from ...bus.scheduler.cancel import Cancelled
from .accumulator import StreamAccumulator
from .cache import LlmCache, request_hash
from .canonical import CanonicalError, LlmRequest, ReasoningDelta, TextDelta
from .errors import AdapterProtocolError, code_for, policy_for

#: Stable ``location`` strings embedded in ``error_info`` (LLM failures).
LOCATION_ON_REQUEST = "linteum_agent.runtime.llm.executor.LLMExecutor.on_request"
LOCATION_STREAM = "linteum_agent.runtime.llm.executor.LLMExecutor._stream_once"

#: Internal sentinel: the stream was *cooperatively* cancelled (the session's
#: :class:`CancelToken` was tripped).  Unlike a provider error it must stop the
#: request outright — no retry, no fallback (§16.3).
_CANCELLED = object()

#: Watchdog budget the executor declares for its own ``llm.request`` handler.
#: A model stream is **unbounded** (thinking + long answers), so the bus' generic
#: 5 s handler cap would falsely cancel it as ``E_CANCELLED``.  The executor knows
#: it is long-running, so it registers this budget with the bus (``set_handler_timeout``)
#: instead of forcing every host to raise the global cap.  It still bounds a truly
#: stuck handler; each SDK attempt keeps its own ``LLMConfig.timeout``.
DEFAULT_REQUEST_HANDLER_TIMEOUT = 600.0


def redact_request(body: dict) -> dict:
    """A safe, JSON-serialisable summary of the outgoing SDK kwargs (§37.3).

    Message *content* is deliberately excluded — the preview answers "which model
    / how many inputs / which tools", it does not leak prompt data.
    """
    tools = body.get("tools") or []
    names = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        target = fn or tool
        names.append(target.get("name") if isinstance(target, dict) else None)
    return {
        "model": body.get("model"),
        "stream": body.get("stream"),
        "n_input": len(body.get("messages") or body.get("input") or []),
        "n_tools": len(tools),
        "tool_names": [n for n in names if n],
        "max_tokens": body.get("max_tokens", body.get("max_output_tokens")),
    }


def exception_detail(exc: BaseException, limit: int = 400) -> str:
    """Human-readable provider detail for ``llm.failed`` (§41).

    Without this, a failed call only tells you the canonical bucket
    (e.g. ``BAD_REQUEST``) and loses the provider's actual message
    (e.g. ``Model Not Exist``), which is exactly what you need to fix it.
    """
    text = str(exc) or type(exc).__name__
    body = getattr(exc, "body", None)
    if body is not None:
        try:
            rendered = json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else str(body)
        except Exception:  # noqa: BLE001
            rendered = str(body)
        if rendered and rendered not in text:
            text = f"{text} | body={rendered}"
    return text if len(text) <= limit else text[:limit] + "…"


class LLMExecutor:
    def __init__(self, bus, router, *, cache: Optional[LlmCache] = None, cancel=None,
                 request_timeout: Optional[float] = DEFAULT_REQUEST_HANDLER_TIMEOUT) -> None:
        self.bus = bus
        self.router = router
        self.cache = cache if cache is not None else LlmCache()
        self.cancel = cancel
        self.last_failure: Optional[dict] = None
        # Decouple the (unbounded) model stream from the bus' generic handler watchdog:
        # declare our own budget so a long stream is never falsely cancelled.  Hosts can
        # override via ``request_timeout=`` (None = disable the cap entirely).
        register_budget = getattr(bus, "set_handler_timeout", None)
        if register_budget is not None and request_timeout is not None:
            register_budget("llm.request", request_timeout)
        bus.on("llm.request", self.on_request)

    # -- entry point --------------------------------------------------------
    async def on_request(self, ev: Event) -> None:
        req = LlmRequest.from_event(ev)
        candidates = self.router.candidates(req, ev.session_id)
        if not candidates:
            await self._fail(ev, "E_NO_ADAPTER", f"no candidate for alias {req.model!r}",
                             location=LOCATION_ON_REQUEST)
            return

        last: Optional[tuple] = None
        for provider, model, adapter in candidates:
            capability = adapter.capabilities(model)
            # ★ 别名（req.model，如 "default"）必须替换为链上解析出的真实模型，
            #   否则会把别名当作 model 发给 provider（DeepSeek 会回 400 Model Not Exist）。
            scoped = self._clamp(req, capability,
                                 default_max_tokens=getattr(adapter.config, "max_tokens", None)
                                 ).replace(model=model)
            body, dropped = adapter.build_body(scoped)
            if dropped:
                await self.bus.publish(
                    ev.child("llm.params.dropped",
                             {"keys": dropped, "model": model, "provider": provider})
                )
            await self.bus.publish(ev.child("llm.started", {
                "provider": provider,
                "model": model,
                "alias": req.model,
                "adapter_version": adapter.adapter_version,
                "request_preview": redact_request(body),
                "context_digest": ev.payload.get("context_digest"),
                "round": ev.payload.get("round"),
            }))
            outcome = await self._run_candidate(ev, adapter, model, scoped, provider)
            if outcome is None:
                self.router.pin(ev.session_id, provider, model)  # I25
                return
            if outcome is _CANCELLED:
                return  # cooperatively cancelled: llm.failed already emitted, stop
            error, code, detail, exc = outcome
            policy = policy_for(error)
            if policy.breaker_count and self.router.breaker is not None:
                self.router.breaker.record((provider, model), ok=False)
            if policy.isolate:
                await self.bus.publish(ev.child("system.provider.quarantined",
                                                {"provider": provider, "model": model}))
            last = (code, detail, exc)
            if policy.fallback:
                continue
            await self._fail(ev, code, f"{provider}/{model}: {code}: {detail}",
                             provider=provider, model=model, exc=exc, detail=detail,
                             location=LOCATION_ON_REQUEST)
            return

        code, detail, exc = last if last else ("E_NO_ADAPTER", "", None)
        await self._fail(ev, code, f"all candidates failed ({code}: {detail})",
                         exc=exc, detail=detail, location=LOCATION_ON_REQUEST)

    # -- one candidate, with its retry budget ------------------------------
    async def _run_candidate(self, ev, adapter, model, req, provider):
        attempt = 0
        while True:
            outcome = await self._stream_once(ev, adapter, model, req, provider)
            if outcome is None:
                return None
            if outcome is _CANCELLED:
                return _CANCELLED  # cooperative cancel: no retry, no fallback
            error, code, detail, exc = outcome
            policy = policy_for(error)
            if attempt < policy.max_retries:
                if policy.base_backoff:
                    await asyncio.sleep(policy.base_backoff * (2 ** attempt))
                attempt += 1
                continue
            return error, code, detail, exc

    async def _stream_once(self, ev, adapter, model, req, provider):
        capability = adapter.capabilities(model)
        key = request_hash(req, adapter, model)
        cached = self.cache.get(key) if self.cache is not None else None
        acc = StreamAccumulator(
            adapter=adapter, model=model, reasoning_policy=capability.reasoning_history_policy
        )
        try:
            if cached is not None:
                # replay path: consume the cache, never call the provider again (§44.1)
                await self._publish_completed(ev, adapter, model, acc, cached)
                return None
            token = self._token(ev.session_id)
            async for chunk in adapter.astream(req):
                if token is not None and hasattr(token, "raise_if_cancelled"):
                    token.raise_if_cancelled()  # cooperative cancel (§16.3)
                acc.feed(chunk)
                for out in self._to_events(ev, chunk):
                    await self.bus.publish(out)
        except Cancelled:  # cooperative token cancel (§16.3) — a user interrupt
            await self._publish_interrupted(ev, adapter, model, acc)
            return _CANCELLED
        except asyncio.CancelledError as exc:  # I19 — never swallow cancellation
            # The bus' watchdog flags its own timeout on the task (``_handler_timeout``);
            # a plain cancellation (user/session) leaves it unset.  Report accordingly so
            # a *local* timeout is not mislabeled as a user cancel.
            timed_out = bool(getattr(asyncio.current_task(), "_handler_timeout", False))
            await self._fail(
                ev,
                "E_TIMEOUT" if timed_out else "E_CANCELLED",
                "handler timeout" if timed_out else "cancelled",
                exc=exc, provider=provider, model=model, location=LOCATION_STREAM,
                retryable=timed_out, enters_history=False, swallow=True,
            )
            raise
        except AdapterProtocolError as exc:
            return (CanonicalError.PROTOCOL, code_for(CanonicalError.PROTOCOL),
                    exception_detail(exc), exc)
        except Exception as exc:  # noqa: BLE001 - everything becomes a failed event (P4)
            error = adapter.classify_error(exc)
            return error, code_for(error), exception_detail(exc), exc

        response = acc.to_response()
        if self.cache is not None:
            self.cache.put(key, response)
        await self._publish_completed(ev, adapter, model, acc, response)
        return None

    # -- event shaping ------------------------------------------------------
    def _to_events(self, ev: Event, chunk) -> list:
        """Only *text* chunks become events; tool args/usage stay in the accumulator (§44)."""
        if isinstance(chunk, TextDelta) and chunk.text:
            return [ev.child("llm.token.delta", {"text": chunk.text})]
        if isinstance(chunk, ReasoningDelta) and chunk.text:
            return [ev.child("llm.reasoning.delta", {"text": chunk.text})]  # never into messages (I23)
        return []

    async def _publish_completed(self, ev: Event, adapter, model, acc, response) -> None:
        cost = adapter.estimate_cost(response.usage, model)
        tool_calls = [
            {"call_id": call.id, "name": call.name, "args": call.arguments}
            for call in response.message.tool_calls
        ]
        await self.bus.publish(ev.child("llm.completed", {
            "message": response.message.to_dict(),
            "tool_calls": tool_calls,
            "finish": response.finish,
            "usage": response.usage.to_dict(),
            "provider": response.provider,
            "model_actual": response.model_actual,
            "adapter_version": response.adapter_version,
            "reasoning_policy": acc.reasoning_policy,
            "cost": cost.to_dict(),
            "pricing_version": cost.pricing_version,
            "round": ev.payload.get("round"),
        }))

    async def _publish_interrupted(self, ev: Event, adapter, model, acc) -> None:
        """Emit the partial assistant message as an ``interrupted`` completion (§16.3).

        The stream was cut short by a user interrupt, so instead of a failure we
        surface what the model produced *before* the interrupt (text only) tagged
        with ``interrupted``/``finish:"interrupted"``.  The executor layer stays
        authoritative for the partial, so the reducer never sees raw deltas (I4).
        """
        partial = acc.partial_message()
        await self.bus.publish(ev.child("llm.completed", {
            "message": partial.to_dict(),
            "tool_calls": [],
            "finish": "interrupted",
            "interrupted": True,
            "usage": acc.usage.to_dict(),
            "provider": adapter.provider,
            "model_actual": acc.model_actual,
            "adapter_version": adapter.adapter_version,
            "reasoning_policy": acc.reasoning_policy,
            "round": ev.payload.get("round"),
        }))

    # -- helpers ------------------------------------------------------------
    def _clamp(self, req: LlmRequest, capability, *, default_max_tokens: Optional[int] = None) -> LlmRequest:
        """Resolve the effective ``max_tokens`` then clamp it to the model limit.

        ``LlmRequest.max_tokens`` is optional: when the request does not carry one,
        the adapter config's ``max_tokens`` (e.g. ``LLMConfig(max_tokens=…)``) is
        used.  The resolved value is always clamped to the model's declared
        ``capability.max_output`` so a too-large number cannot 400 at the provider.
        """
        wanted = req.max_tokens if req.max_tokens is not None else default_max_tokens
        if capability.max_output and wanted and wanted > capability.max_output:
            wanted = capability.max_output
        if wanted is not None and wanted != req.max_tokens:
            return req.replace(max_tokens=wanted)
        return req

    def _token(self, session_id: str):
        if self.cancel is None:
            return None
        token_for = getattr(self.cancel, "token_for", None)
        return token_for(session_id) if token_for else None

    async def _fail(self, ev: Event, code: str, message: str, *,
                    exc: Optional[BaseException] = None,
                    provider: Optional[str] = None, model: Optional[str] = None,
                    location: Optional[str] = None, detail: Optional[str] = None,
                    retryable: bool = False, enters_history: bool = True,
                    swallow: bool = False) -> None:
        """Publish ``llm.failed`` with a structured ``error_info`` (source=llm).

        The payload keeps ``code``/``error`` and adds *where* (``location``), the
        **original provider error** (``detail`` from :func:`exception_detail`) and
        the provider/model — so ``E_TIMEOUT`` from the adapter is never confused
        with ``E_TIMEOUT`` from a tool.
        """
        context = {k: v for k, v in (("provider", provider), ("model", model)) if v}
        info = error_info(
            code, source=ErrorSource.LLM.value, location=location or LOCATION_ON_REQUEST,
            message=message, exc=exc, detail=detail,
            retryable=retryable, enters_history=enters_history, context=context,
        ).to_dict()
        payload = {"code": code, "error": message, "error_info": info}
        self.last_failure = {"code": code, "error": message, "session_id": ev.session_id,
                             "error_info": info}
        if swallow:
            try:
                await self.bus.publish(ev.child("llm.failed", payload))
            except Exception:  # noqa: BLE001 - we are already being cancelled
                pass
            return
        await self.bus.publish(ev.child("llm.failed", payload))

