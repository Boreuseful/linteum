"""LLM-layer errors, canonical error codes and the retry/fallback matrix (§41).

The executor needs two things from an error: *which* canonical bucket it is
(:class:`~linteum_agent.runtime.llm.canonical.CanonicalError`) and *what to do about it*
(retry? fall back? trip the breaker? isolate the provider?).  Both live here so
the policy table is a single, reviewable place — mirroring §41.3.
"""

from __future__ import annotations

from dataclasses import dataclass

from .canonical import CanonicalError


class LLMError(Exception):
    """Base class for adapter/executor errors (all become ``llm.failed`` events)."""


class ProviderError(LLMError):
    """A non-2xx response (or transport failure) from a provider."""

    def __init__(self, message: str = "", *, status: int = 0, body: bytes = b"") -> None:
        super().__init__(message or f"provider error (status={status})")
        self.status = status
        self.body = body


class AdapterProtocolError(LLMError):
    """Stream parsing failed / fields missing / duplicate call ids (I26, §37.4)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class UnsupportedCapability(LLMError):
    """The chosen model does not satisfy ``route.require`` (E_ADAPTER_UNSUPPORTED)."""


class DuplicateAdapter(LLMError):
    """Two adapters registered for the same provider."""


class NoAdapter(LLMError):
    """An alias resolved to no candidate (E_NO_ADAPTER)."""


# ---------------------------------------------------------------------------
# CanonicalError -> Appendix A error code
# ---------------------------------------------------------------------------

ERROR_CODE: dict = {
    CanonicalError.AUTH: "E_LLM_AUTH",
    CanonicalError.RATE_LIMIT: "E_RATE_LIMITED",
    CanonicalError.CONTEXT_OVERFLOW: "E_CONTEXT_OVERFLOW",
    CanonicalError.CONTENT_FILTER: "E_LLM_CONTENT_FILTER",
    CanonicalError.BAD_REQUEST: "E_BAD_ARGS",
    CanonicalError.PROVIDER_DOWN: "E_LLM_PROVIDER_UNAVAILABLE",
    CanonicalError.OVERLOADED: "E_LLM_OVERLOADED",
    CanonicalError.TIMEOUT: "E_TIMEOUT",
    CanonicalError.UNSUPPORTED: "E_ADAPTER_UNSUPPORTED",
    CanonicalError.PROTOCOL: "E_ADAPTER_PROTOCOL",
    CanonicalError.UNKNOWN: "E_LLM_PROVIDER_UNAVAILABLE",
}


@dataclass(frozen=True)
class RetryPolicy:
    """How the executor reacts to one canonical error class (§41.3)."""

    max_retries: int = 0
    base_backoff: float = 0.0
    fallback: bool = False       # may move to the next candidate in the chain
    breaker_count: bool = False  # counts toward the provider circuit breaker
    isolate: bool = False        # quarantine the provider (needs ops attention)


#: The §41.3 matrix, verbatim.
RETRY_MATRIX: dict = {
    CanonicalError.TIMEOUT: RetryPolicy(1, 0.0, fallback=True, breaker_count=True),
    CanonicalError.RATE_LIMIT: RetryPolicy(3, 0.5, fallback=True, breaker_count=True),
    CanonicalError.OVERLOADED: RetryPolicy(2, 0.5, fallback=True, breaker_count=True),
    CanonicalError.PROVIDER_DOWN: RetryPolicy(2, 0.5, fallback=True, breaker_count=True),
    CanonicalError.CONTEXT_OVERFLOW: RetryPolicy(1, 0.0, fallback=False),
    CanonicalError.AUTH: RetryPolicy(0, 0.0, fallback=False, breaker_count=True, isolate=True),
    CanonicalError.CONTENT_FILTER: RetryPolicy(0, 0.0, fallback=False),
    CanonicalError.BAD_REQUEST: RetryPolicy(0, 0.0, fallback=False),
    CanonicalError.UNSUPPORTED: RetryPolicy(0, 0.0, fallback=True),
    CanonicalError.PROTOCOL: RetryPolicy(0, 0.0, fallback=False, breaker_count=True),
    CanonicalError.UNKNOWN: RetryPolicy(0, 0.0, fallback=False),
}


def policy_for(error: CanonicalError) -> RetryPolicy:
    return RETRY_MATRIX.get(error, RetryPolicy())


def code_for(error: CanonicalError) -> str:
    return ERROR_CODE.get(error, "E_LLM_PROVIDER_UNAVAILABLE")
