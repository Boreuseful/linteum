"""LLM response cache keyed by ``request_hash`` (§12, §44.1).

The hash includes ``provider`` + ``adapter_version`` + ``model`` (I28): the same
canonical request can legitimately produce different results across providers or
adapter versions, so an idempotency key that ignored them would be wrong.  The
cache also gives the executor its replay path — a hit means "do not call the
provider again" (§44.1).
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from .canonical import LlmRequest, LlmResponse


def request_hash(req: LlmRequest, adapter, model: str) -> str:
    """Deterministic idempotency key (I28)."""
    payload = {
        "provider": adapter.provider,
        "adapter_version": adapter.adapter_version,  # ★ required by I28
        "model": model,
        "req": req.to_dict(),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class LlmCache:
    """In-memory result cache.  A real deployment backs this with the WAL/artifact."""

    def __init__(self, *, max_size: int = 10_000) -> None:
        self.max_size = max_size
        self._store: dict = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[LlmResponse]:
        response = self._store.get(key)
        if response is None:
            self.misses += 1
        else:
            self.hits += 1
        return response

    def put(self, key: str, response: LlmResponse) -> None:
        if len(self._store) >= self.max_size:
            self._store.pop(next(iter(self._store)))
        self._store[key] = response

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)
