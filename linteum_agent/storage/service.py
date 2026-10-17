"""Session read model: list, history and the current context plan.

The service deliberately contains no reducer logic.  History is
``latest checkpoint + WAL tail`` replayed through :meth:`Agent.replay_state`,
which is the same projection used by recovery.
"""

from __future__ import annotations

import time
from typing import Optional, Sequence

from ..bus.artifact import NullArtifactStore, hydrate_events
from ..bus.event import uuid7
from ..runtime.agent import Agent
from ..runtime.context import (
    CompactingContextBuilder,
    ContextBuilder,
    ContextPlan,
    ContextPolicy,
    PassthroughContextBuilder,
)
from ..runtime.state.agent_state import AgentState
from .base import SessionInfo, StorageBundle


class SessionService:
    """Host-facing read API over a :class:`StorageBundle`."""

    def __init__(
        self,
        storage: StorageBundle,
        *,
        builder: Optional[ContextBuilder] = None,
        policy: Optional[ContextPolicy] = None,
    ) -> None:
        self.storage = storage
        if builder is not None:
            self.builder = builder
        elif policy is not None and policy.enabled:
            self.builder = CompactingContextBuilder()
        else:
            self.builder = PassthroughContextBuilder()
        self.policy = policy

    # -- catalogue ----------------------------------------------------------
    async def create_session(
        self,
        session_id: str,
        *,
        title: Optional[str] = None,
        workspace: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> SessionInfo:
        current = await self.storage.catalog.get_session(session_id)
        if current is not None:
            return current
        now = time.time()
        info = SessionInfo(
            session_id=session_id,
            created_at=now,
            updated_at=now,
            title=title,
            workspace=workspace,
            metadata=dict(metadata or {}),
        )
        await self.storage.catalog.upsert_session(info)
        return info

    async def list_sessions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        workspace: Optional[str] = None,
    ) -> list[SessionInfo]:
        return await self.storage.catalog.list_sessions(
            limit=limit, offset=offset, workspace=workspace
        )

    async def get_session(self, session_id: str) -> Optional[SessionInfo]:
        return await self.storage.catalog.get_session(session_id)

    async def delete_session(self, session_id: str) -> None:
        await self.storage.catalog.delete_session(session_id)
        if self.storage.checkpointer is not self.storage.catalog:
            remove = getattr(self.storage.checkpointer, "delete_session", None)
            if remove is not None:
                result = remove(session_id)
                if hasattr(result, "__await__"):
                    await result

    # -- history / context --------------------------------------------------
    async def _state_at(self, session_id: str, *, include_inflight: bool) -> AgentState:
        checkpoint = self.storage.checkpointer.latest(session_id)
        events = await self.storage.wal.replay(
            session_id=session_id,
            after_seq=checkpoint.seq if checkpoint is not None else 0,
        )
        if checkpoint is not None:
            state = AgentState.from_dict(checkpoint.state)
            state.last_event_seq = max(state.last_event_seq, checkpoint.seq)
        else:
            trace_id = events[0].trace_id if events else uuid7()
            state = AgentState(session_id=session_id, trace_id=trace_id)
        if include_inflight and events:
            artifacts = self.storage.artifacts
            if artifacts is None:
                artifacts = NullArtifactStore()
            events = await hydrate_events(events, artifacts)
            state = await Agent.replay_state(state, events)
        return state

    async def history(
        self, session_id: str, *, include_inflight: bool = True
    ) -> list[dict]:
        state = await self._state_at(session_id, include_inflight=include_inflight)
        return list(state.messages)

    async def context(
        self, session_id: str, *, tools: Sequence = ()
    ) -> ContextPlan:
        state = await self._state_at(session_id, include_inflight=True)
        return await self.builder.build(state, tools=tools)

    # -- lifecycle ----------------------------------------------------------
    async def restore_agent(self, bus, *, session_id: str, **kwargs) -> Agent:
        kwargs.pop("checkpointer", None)
        return await Agent.restore(
            bus, self.storage.checkpointer, session_id=session_id, **kwargs
        )


__all__ = ["SessionService"]
