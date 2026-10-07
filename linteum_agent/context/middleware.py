from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

try:
    from ..model.client import LLMClient
    from ..model.llm import LLMResponse, LLMStreamChunk
    from ..messages.agent_message import (
        AssistantMessage,
        Message,
        MessageInput,
        MessageInputs,
        ToolCall,
        UserPrompt,
        normalize_messages,
        to_dict,
    )
    from .store import ContextStore
    from .session import Round, Session, empty_usage, utc_now
except ImportError:  # supports adding /workspace/linteum_agent to sys.path directly
    from model.client import LLMClient
    from model.llm import LLMResponse, LLMStreamChunk
    from messages.agent_message import (
        AssistantMessage,
        Message,
        MessageInput,
        MessageInputs,
        ToolCall,
        UserPrompt,
        normalize_messages,
        to_dict,
    )
    from store import ContextStore
    from session import Round, Session, empty_usage, utc_now

ContextInjector = Callable[[str, list[Message]], Iterable[MessageInput]]


class ContextMiddleware:
    """Inject context before the LLM request and automatically save conversation messages after the response.

    Injection order: static injected content -> dynamic injector content -> persisted history ->
    current request. The injected content (system prompt, etc.) is placed **before** the history so
    that "the assistant's tool_call" and "the tool result in the current request" always stay adjacent:
    the Responses API requires ``function_call`` to be immediately followed by ``function_call_output``,
    with no other message inserted in between, otherwise it raises
    ``No tool output found for tool call ...``.

    What ``store`` holds is a ``Session`` (workspace + context representation + metadata), not
    provider-specific JSON; each session's messages are ``Session.context``. Written messages and usage
    are merged into the currently ongoing round (``begin_round()`` / ``end_round()``; **one round = one
    complete agent-loop**); when there is no ongoing round, a single ``complete()`` / ``stream()`` is
    one round.
    """

    def __init__(
        self,
        client: LLMClient,
        store: Any | None = None,
        *,
        context: MessageInputs | None = None,
        injector: ContextInjector | None = None,
        persist_user_messages: bool = True,
        workspace: str | Path | None = None,
    ):
        self.client = client
        self.store = store or ContextStore(workspace=workspace)
        self.workspace = (
            Path(workspace).expanduser().resolve() if workspace is not None else self.store.default_workspace
        )
        self.context = normalize_messages(context or [])
        self.injector = injector
        self.persist_user_messages = persist_user_messages

    def _messages(self, session_id: str, messages: MessageInputs) -> list[Message]:
        history = self.store.get(session_id)
        injected = list(self.context)
        if self.injector:
            injected.extend(normalize_messages(self.injector(session_id, history)))
        # Injected content goes before the history: this way "the assistant's tool_call (at the end of
        # the history)" and "the tool result in the current request" are not separated by injected system
        # messages -- the Responses API requires function_call to be immediately followed by
        # function_call_output.
        return injected + history + normalize_messages(messages)

    @staticmethod
    def _response_message(response: LLMResponse) -> AssistantMessage:
        return AssistantMessage(
            content=response.text or None,
            tool_calls=response.tool_calls,
            reasoning_content=response.reasoning_content or None,
        )

    def _save_turn(
        self,
        session_id: str,
        messages: MessageInputs,
        response: LLMResponse,
        *,
        created_at: str | None = None,
    ) -> None:
        """Write "this request's input + this reply" into the session.

        This group of messages and ``response.usage`` are merged into the currently ongoing round (the
        complete agent-loop opened by ``begin_round()``); when there is no ongoing round (using the
        Middleware standalone) it is finalized into one round immediately. The round's ``usage``
        summarizes this round's token consumption (also accumulated into ``session.usage``), the context
        space occupied by this round (``context_tokens`` / ``context_chars``), and the start/end time.
        """
        session = self.ensure_session(session_id)
        items: list[Message] = []
        if self.persist_user_messages:
            items.extend(normalize_messages(messages))
        items.append(self._response_message(response))
        session.record_round(items, response.usage, created_at=created_at)

    def get_context(self, session_id: str = "default") -> list[Message]:
        return self.store.get(session_id)

    def clear_context(self, session_id: str = "default") -> None:
        self.store.clear(session_id)

    def append_tool_results(self, session_id: str, results: MessageInputs) -> None:
        self.store.append(session_id, results)

    def complete(self, messages: MessageInputs, *, session_id: str = "default", **kwargs: Any) -> LLMResponse:
        started_at = utc_now()
        response = self.client.complete(self._messages(session_id, messages), session_id=session_id, **kwargs)
        self._save_turn(session_id, messages, response, created_at=started_at)
        return response

    def stream(self, messages: MessageInputs, *, session_id: str = "default", **kwargs: Any) -> Iterator[LLMStreamChunk]:
        started_at = utc_now()
        final: LLMResponse | None = None
        for chunk in self.client.stream(self._messages(session_id, messages), session_id=session_id, **kwargs):
            if chunk.done and chunk.response:
                final = chunk.response
            yield chunk
        if final:
            self._save_turn(session_id, messages, final, created_at=started_at)

    async def acomplete(self, messages: MessageInputs, *, session_id: str = "default", **kwargs: Any) -> LLMResponse:
        started_at = utc_now()
        response = await self.client.acomplete(self._messages(session_id, messages), session_id=session_id, **kwargs)
        self._save_turn(session_id, messages, response, created_at=started_at)
        return response

    async def astream(self, messages: MessageInputs, *, session_id: str = "default", **kwargs: Any) -> AsyncIterator[LLMStreamChunk]:
        started_at = utc_now()
        final: LLMResponse | None = None
        async for chunk in self.client.astream(self._messages(session_id, messages), session_id=session_id, **kwargs):
            if chunk.done and chunk.response:
                final = chunk.response
            yield chunk
        if final:
            self._save_turn(session_id, messages, final, created_at=started_at)

    # ------------------------------------------------------------------
    # Session (workspace + context representation + metadata)
    # ------------------------------------------------------------------

    def ensure_session(
        self,
        session_id: str = "default",
        *,
        workspace: str | Path | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        """Get the session, creating it with the middleware's default workspace if it does not exist."""
        return self.store.ensure_session(
            session_id,
            workspace=self.workspace if workspace is None else workspace,
            metadata=metadata,
        )

    def get_session(self, session_id: str = "default") -> Session | None:
        """Return the session object; ``None`` if it does not exist (does not create a session)."""
        return self.store.get_session(session_id)

    def session_workspace(self, session_id: str = "default") -> Path:
        """Return the effective workspace of the session."""
        return self.store.workspace(session_id)

    def set_workspace(self, session_id: str, workspace: str | Path | None) -> Session:
        """Set the session workspace; ``None`` restores "inherited at runtime"."""
        return self.store.set_workspace(session_id, workspace)

    def get_session_usage(self, session_id: str = "default") -> dict[str, int]:
        """Return the session's cumulative token usage (input/output/cache_hit/total).

        When the session exists, the separate field ``session.usage`` (persistable) wins, so after
        ``reset_session_usage()`` the value read is the zeroed result; when the session does not exist it
        falls back to the ``LLMClient`` counter.
        """
        session = self.store.get_session(session_id)
        if session is not None:
            usage = empty_usage()
            usage.update(session.usage or {})
            return usage
        return dict(self.client.get_session_usage(session_id))

    def reset_session_usage(self, session_id: str = "default") -> Session | None:
        """Zero the session's cumulative token usage; return ``None`` if the session does not exist."""
        session = self.store.get_session(session_id)
        if session is not None:
            session.reset_usage()
        return session

    def get_session_rounds(self, session_id: str = "default") -> list[Round]:
        """Return the session's per-round records: each round ``{context, usage}``; empty list if the session does not exist.

        One round = one complete agent-loop: ``context`` is all of this round's messages (request + every
        LLM call + tool results), and ``usage`` is this round's input/output token summary, the context
        space this round occupies (``context_tokens`` / ``context_chars``), and this round's start/end
        time (``created_at`` / ``updated_at``).
        """
        session = self.store.get_session(session_id)
        return list(session.rounds) if session is not None else []

    # ------------------------------------------------------------------
    # Rounds (round): one round = one complete agent-loop
    # ------------------------------------------------------------------

    def begin_round(self, session_id: str = "default") -> Round:
        """Start one complete conversation round (one round = one agent-loop); reuse an ongoing round directly.

        ``AgentEngine`` calls this method before the tool loop starts and ``end_round()`` after the loop
        ends, so this round covers "one request -> multiple LLM calls + tool execution -> final answer";
        messages and usage written by ``complete()`` / ``stream()`` during this period are merged into
        this round.
        """
        return self.ensure_session(session_id).begin_round()

    def end_round(self, session_id: str = "default") -> Round | None:
        """End the ongoing round (finalize this round's messages + usage into the session); return ``None`` if none."""
        session = self.store.get_session(session_id)
        return session.end_round() if session is not None else None

    def open_round(self, session_id: str = "default") -> Round | None:
        """Return the ongoing round (not yet ``end_round()``); ``None`` if none."""
        session = self.store.get_session(session_id)
        return session.open_round if session is not None else None
