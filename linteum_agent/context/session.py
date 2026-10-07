"""Session state: the conversation's workspace, context representation, extra metadata, Tool/Skill config, and token usage.

The Context layer uses ``Session`` to represent the full state of one session:

```text
Session
├── session_id      session ID
├── workspace       session workspace (the root directory for tool execution)
├── context         context representation (a provider-neutral message list)
├── metadata        session-level extra info (business-defined; does not hold the reserved config below)
├── tools           tool config: which tools this session registered + enable/disable/options
├── skills          Skill config: which Skills this session registered + enable/disable/options
├── usage           cumulative token usage: input/output/cache_hit/total
├── approve         tool approval callback
└── cancelled       cancellation check callback
```

``tools`` / ``skills`` / ``usage`` are **separate fields** (no longer embedded in ``metadata``);
``tools`` / ``skills`` share the same structure::

    session.tools = {
        "registered": ["read_file", "bash"],       # tools registered (available) for this session
        "enabled": ["read_file"],                  # allowlist (takes effect when non-empty)
        "disabled": ["bash"],                      # denylist (higher precedence)
        "options": {"bash": {"timeout": 60}},      # per-tool options
        "timeout": 30,                             # other keys are global options
    }
    session.skills = {
        "registered": ["python-code-review"],      # Skills registered (available) for this session
        "enabled": [...],
        "disabled": [...],
        "options": {...},
    }

``registered`` only records "which tools/Skills this session has", decoupled from ``enabled`` /
``disabled`` (whether they may be used): ``ToolRegistry`` writes it automatically when registering
tools, ``SkillRegistry.bind_session()`` writes the discovered Skills; callers can also register
directly via ``Session.register_tools()`` / ``Session.register_skills()``.

The session also accumulates the token usage of each LLM call::

    session.usage = {
        "input_tokens": 150,        # cumulative input tokens
        "output_tokens": 30,        # cumulative output tokens
        "cache_hit_tokens": 45,     # cumulative cache-hit tokens
        "total_tokens": 180,        # input + output
    }

``ContextMiddleware`` calls ``Session.record_usage()`` after each completed LLM call, so
``session.usage`` directly reads this session's cumulative usage and is persisted along with the
session JSON.

Besides the cumulative usage, **one complete agent-loop is wrapped as a ``Round``**
(``session.rounds``): ``Round.context`` is all of this round's messages (request + each LLM call +
tool results), and ``Round.usage`` is this round's token summary and occupied context space.
``to_dict()`` therefore serializes ``messages`` as a **list of rounds**::

    "messages": [
        {
            "round": 1,
            "context": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hello, how can I help you?"},
            ],
            "usage": {
                "input_tokens": 12,       # this round's input tokens
                "output_tokens": 8,       # this round's output tokens
                "cache_hit_tokens": 0,    # this round's cache-hit tokens
                "total_tokens": 20,       # input + output
                "context_tokens": 20,     # context space occupied by this round (estimated token count)
                "context_chars": 42,      # context space occupied by this round (character count)
                "created_at": "2026-01-01T00:00:00+00:00",   # this round's start time
                "updated_at": "2026-01-01T00:00:05+00:00",   # this round's end time
            },
        },
    ]

A round's lifecycle is ``begin_round()`` -> multiple ``record_round()`` -> ``end_round()``:

- ``AgentEngine`` (``run`` / ``arun`` / ``stream`` / ``astream``) calls ``begin_round()`` before the
  tool loop starts and ``end_round()`` after the loop ends (including on exception or a max-rounds
  abort), so one user request "multiple LLM calls + tool execution + final answer" occupies only
  **one round**;
- When using ``ContextMiddleware`` standalone there is no loop, so a single ``complete()`` /
  ``stream()`` is naturally one round (``record_round()`` finalizes it immediately);
- ``record_round()`` appends messages to ``context`` and accumulates tokens into ``session.usage``
  and the current round's ``usage``; ``Session.record_usage()`` can still update the cumulative usage
  separately;
- An in-progress round that has not yet ``end_round()``-ed is also output by ``to_dict()`` at its
  current progress, so persisting mid-way never loses messages.

Reading the old format (a flat message list) automatically splits/wraps it by round.

Backward compatibility: ``tools`` / ``skills`` / ``usage`` were once embedded in ``metadata``; when
constructing a ``Session`` or reading via ``Session.from_dict()`` they are automatically migrated to
separate fields.

The tool execution context (the former ``linteum_agent.tools.RuntimeContext``) has been merged into
``Session``: ``workspace`` / ``metadata`` / ``approve`` / ``cancelled`` / ``log()`` /
``is_cancelled()`` are all provided by Session, and ``ToolRegistry`` reads the tool config in the
Session directly. The ``tools.RuntimeContext`` name is still available, but it is only a
compatibility alias for ``Session``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

try:
    from ..messages.agent_message import (
        Message,
        MessageInput,
        MessageInputs,
        normalize_messages,
        to_dict as message_to_dict,
    )
except ImportError:  # supports adding /workspace/linteum_agent to sys.path directly
    from messages.agent_message import (
        Message,
        MessageInput,
        MessageInputs,
        normalize_messages,
        to_dict as message_to_dict,
    )

ApproveFn = Callable[[str, dict[str, Any]], bool]
CancelFn = Callable[[], bool]

# Field name / JSON key / old metadata key for tool config: session.tools.
TOOL_CONFIG_KEY = "tools"
# Field name / JSON key / old metadata key for Skill config: session.skills.
SKILL_CONFIG_KEY = "skills"
# The key in each capability config that records "which tools/Skills this session registered".
REGISTERED_KEY = "registered"

# Field name / JSON key / old metadata key for token usage: session.usage.
USAGE_KEY = "usage"
# The session's cumulative token metrics; total_tokens is computed as input + output, consistent with LLMClient.
USAGE_METRICS = ("input_tokens", "output_tokens", "cache_hit_tokens", "total_tokens")

# Round persistence fields: each item in ``messages`` is a Round.
# The round-index key (the 1 in "round_1"), this round's message key, and this round's occupied context-space metrics.
ROUND_INDEX_KEY = "round"
ROUND_CONTEXT_KEY = "context"
# Context space occupied by this round: estimated token count and character count.
CONTEXT_TOKENS_KEY = "context_tokens"
CONTEXT_CHARS_KEY = "context_chars"
# Fields in a round's usage besides the token metrics: occupied context space + this round's start/end time.
ROUND_SPACE_METRICS = (CONTEXT_TOKENS_KEY, CONTEXT_CHARS_KEY)
ROUND_TIME_KEYS = ("created_at", "updated_at")

# Reserved fields to move out of metadata (used when migrating old formats).
RESERVED_KEYS = (TOOL_CONFIG_KEY, SKILL_CONFIG_KEY, USAGE_KEY)

# Reserved keys not participating in "global option" merging: registered / enabled / disabled / options.
_RESERVED_CONFIG_KEYS = frozenset({REGISTERED_KEY, "enabled", "disabled", "options"})


def _capability_map(value: Any) -> dict[str, Any]:
    """Normalize the tool/Skill config into a dict; treat a non-Mapping as an empty config."""
    return dict(value) if isinstance(value, Mapping) else {}


def empty_usage() -> dict[str, int]:
    """Return an all-zero token usage whose fields match ``LLMClient`` / ``normalize_usage()``."""
    return {metric: 0 for metric in USAGE_METRICS}


def _coerce_usage(value: Any) -> dict[str, int]:
    """Normalize any usage into a dict containing all metrics (missing ones treated as 0).

    When ``total_tokens`` is missing (or 0) but input/output exist, fill it in as ``input + output``,
    consistent with ``LLMClient`` / ``normalize_usage()``.
    """
    usage = empty_usage()
    if isinstance(value, Mapping):
        for metric in USAGE_METRICS:
            usage[metric] = int(value.get(metric, 0) or 0)
        if not usage["total_tokens"]:
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def _usage_delta(usage: Mapping[str, Any] | None) -> dict[str, int]:
    """Normalize one LLM response's usage into an accumulable delta."""
    if not isinstance(usage, Mapping):
        return empty_usage()
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    cache_hit_tokens = int(usage.get("cache_hit_tokens", 0) or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_hit_tokens": cache_hit_tokens,
        # Consistent with LLMClient / normalize_usage: total = input + output.
        "total_tokens": input_tokens + output_tokens,
    }


def _is_cjk(char: str) -> bool:
    """Determine whether a character is estimated as "one char ~= one token" (CJK, full-width symbols, etc.)."""
    code = ord(char)
    return (
        0x3000 <= code <= 0x303F      # CJK punctuation
        or 0x2E80 <= code <= 0x9FFF   # CJK radicals / Han characters / kana
        or 0xAC00 <= code <= 0xD7AF   # Hangul
        or 0xF900 <= code <= 0xFAFF   # CJK compatibility ideographs
        or 0xFF00 <= code <= 0xFFEF   # full-width characters
    )


def estimate_tokens(text: str) -> int:
    """Roughly estimate the token count of text: 1 token per CJK char, 1 token per 4 other chars.

    No tokenizer dependency is introduced here (different providers use different tokenizers); it is
    only used to record a comparable approximation of "the context space occupied by this round of
    conversation".
    """
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    latin = len(text) - cjk
    return cjk + (latin + 3) // 4


def measure_context(messages: MessageInputs) -> tuple[int, int]:
    """Measure the context space occupied by a batch of messages; return ``(token count, char count)``.

    The measurement uses the internal format of ``to_dict()`` (JSON text), so it includes structural
    overhead such as role / tool_calls and is comparable across rounds.
    """
    text = "".join(
        json.dumps(message_to_dict(message), ensure_ascii=False)
        for message in normalize_messages(messages)
    )
    return estimate_tokens(text), len(text)


def _coerce_round_usage(value: Any) -> dict[str, Any]:
    """Normalize a round's usage: token consumption + occupied context space + start/end time."""
    raw: Mapping[str, Any] = value if isinstance(value, Mapping) else {}
    delta = _usage_delta(raw)
    usage: dict[str, Any] = {metric: delta[metric] for metric in USAGE_METRICS}
    for metric in ROUND_SPACE_METRICS:
        usage[metric] = int(raw.get(metric, 0) or 0)
    for key in ROUND_TIME_KEYS:
        usage[key] = str(raw.get(key) or utc_now())
    return usage


def _is_round_payload(value: Any) -> bool:
    """Determine whether an item in ``messages`` is a round (a normal message has no ``context`` field)."""
    return (
        isinstance(value, Mapping)
        and "role" not in value
        and ROUND_CONTEXT_KEY in value
    )


def _message_role(value: Any) -> str | None:
    """Read the role of a message (either a message object or a dict)."""
    role = getattr(value, "role", None)
    if role is None and isinstance(value, Mapping):
        role = value.get("role")
    return str(role) if role else None


def _flat_round(
    messages: list[Any],
    round: int,
    created_at: str | None,
    updated_at: str | None,
) -> Round:
    """Wrap a segment of flat messages as one round (for old-format migration); token usage is unknown, recorded as 0."""
    tokens, chars = measure_context(messages)
    return Round(
        round=round,
        context=messages,
        usage={
            **empty_usage(),
            CONTEXT_TOKENS_KEY: tokens,
            CONTEXT_CHARS_KEY: chars,
            "created_at": created_at or utc_now(),
            "updated_at": updated_at or utc_now(),
        },
    )


def _rounds_from_flat(
    messages: list[Any],
    *,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> list[Round]:
    """Split a legacy flat message list into rounds "starting at each user message"."""
    rounds: list[Round] = []
    current: list[Any] = []
    for item in messages:
        if current and _message_role(item) == "user":
            rounds.append(_flat_round(current, len(rounds) + 1, created_at, updated_at))
            current = []
        current.append(item)
    if current:
        rounds.append(_flat_round(current, len(rounds) + 1, created_at, updated_at))
    return rounds


def _split_rounds(
    messages: Any,
    *,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> tuple[list[Message], list["Round"]]:
    """Split a session snapshot's ``messages``: the new format is a round list, the old format is a flat message list.

    Return ``(context messages, rounds)``. Old formats (version 1 / 2 / 3) are split into rounds
    "starting at each user message"; when token usage is unknown it is recorded as 0 (occupied space is
    still measured).
    """
    items = list(messages) if isinstance(messages, (list, tuple)) else []
    if not any(_is_round_payload(item) for item in items):
        return list(items), _rounds_from_flat(items, created_at=created_at, updated_at=updated_at)
    rounds: list[Round] = []
    context: list[Message] = []
    for index, item in enumerate(items):
        if _is_round_payload(item):
            round = Round.from_dict(item, round=index + 1)
            rounds.append(round)
            context.extend(round.context)
        else:  # mixed-layout fallback: flat messages are kept in the context
            context.append(item)
    return context, rounds


def _normalize_rounds(value: Any) -> list["Round"]:
    """Normalize any input into ``list[Round]`` (either instantiated or dicts)."""
    rounds: list[Round] = []
    for index, item in enumerate(value if isinstance(value, (list, tuple)) else []):
        if isinstance(item, Round):
            rounds.append(item)
        elif isinstance(item, Mapping):
            rounds.append(Round.from_dict(item, round=index + 1))
    return rounds


@dataclass
class Round:
    """One complete round of conversation (one agent-loop): all of this round's messages (``context``) + this round's aggregated usage.

    ``usage`` records this round's input/output token summary (the sum of the LLM calls within the
    loop), the context space this round occupies (``context_tokens`` / ``context_chars``), and this
    round's start/end time (``created_at`` / ``updated_at``). After serialization it corresponds to an
    item in ``messages``::

        {"round": 1, "context": [...], "usage": {...}}
    """

    round: int = 1
    context: list[Message] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.round = int(self.round or 1)
        self.context = normalize_messages(self.context)
        self.usage = _coerce_round_usage(self.usage)

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable round snapshot."""
        return {
            ROUND_INDEX_KEY: self.round,
            ROUND_CONTEXT_KEY: [message_to_dict(message) for message in self.context],
            USAGE_KEY: dict(self.usage),
        }

    @classmethod
    def from_dict(cls, payload: Any, *, round: int = 1) -> "Round":
        """Restore from a ``to_dict()`` result; treat a non-object ``payload`` as an empty round."""
        if isinstance(payload, Round):
            return payload
        value: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
        return cls(
            round=int(value.get(ROUND_INDEX_KEY) or round or 1),
            context=list(value.get(ROUND_CONTEXT_KEY) or []),
            usage=value.get(USAGE_KEY),
        )


def _normalize_names(values: Any) -> list[str]:
    """Unify ``Tool`` / ``SkillMetadata`` / name strings into a de-duplicated list of names.

    Supports passing a single object, a single name, or any iterable collection of them; the return
    value preserves first-occurrence order, so it can also be used to read a registered list. Raises
    ``ValueError`` when a name cannot be resolved.
    """
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
        values = [values]
    names: list[str] = []
    for value in values:
        name = getattr(value, "name", None)
        if name is None and isinstance(value, str):
            name = value
        if not name:
            raise ValueError(
                f"cannot resolve name: {value!r} (expected Tool/SkillMetadata or a name string)"
            )
        name = str(name)
        if name not in names:
            names.append(name)
    return names


def utc_now() -> str:
    """Return a UTC ISO8601 timestamp, used for the session's creation/update time."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Session:
    """The full state of one session: workspace + context representation + metadata + Tool/Skill config + token usage.

    It is also the tool execution context (the former ``tools.RuntimeContext`` has been merged here): a
    ``workspace`` of ``None`` means "not yet explicitly specified", so at runtime it can inherit the
    tool registry's workspace, and ``effective_workspace`` gives the directory actually in effect;
    ``metadata`` / ``approve`` / ``cancelled`` / ``log()`` / ``is_cancelled()`` are for ``ToolRegistry``
    and tools to use.

    Tool/Skill config and token usage are separate fields (not stored in ``metadata``):

    - ``tools`` / ``skills``: which tools/Skills this session registered (``registered``), the
      enable/disable lists, and the per-tool/Skill options; the common entry points are
      ``register_tools()`` / ``register_skills()``, ``configure_tools()`` / ``configure_skills()``,
      ``registered_tools()`` / ``registered_skills()``, and ``registrations()``.
    - ``usage``: this session's cumulative token usage (input / output / cache_hit / total),
      accumulated by ``record_usage()``, zeroed by ``reset_usage()``, and persisted with the session
      JSON.
    - ``rounds``: per-round records, where each round of conversation is wrapped as a ``Round`` (this
      round's ``context`` + this round's ``usage``: input/output tokens, occupied context space,
      start/end time). **One round = one complete agent-loop**: ``begin_round()`` opens it,
      ``record_round()`` merges into it, ``end_round()`` finalizes it, so one user request (multiple
      LLM calls + tool execution + final answer) occupies only one round; ``to_dict()`` accordingly
      serializes ``messages`` as a round list.

    For backward compatibility with old data, if ``metadata`` still carries ``tools`` / ``skills`` /
    ``usage``, they are automatically migrated to the above fields on construction and removed from
    ``metadata``.
    """

    session_id: str = "default"
    workspace: Path | None = None
    context: list[Message] = field(default_factory=list)
    # Per-round records: the wrapper for each round of conversation (this round's messages + usage). Context = the concatenation of each round's context.
    rounds: list[Round] = field(default_factory=list)
    # The in-progress round (opened by begin_round(), before end_round() finalizes it); not a persistence field.
    _open_round: Round | None = field(default=None, init=False, repr=False, compare=False)
    # Session-level extra info (business-defined; Tool/Skill config and usage use the separate fields below).
    metadata: dict[str, Any] = field(default_factory=dict)
    # Tool config: registered / enabled / disabled / options and global options.
    tools: dict[str, Any] = field(default_factory=dict)
    # Skill config: registered / enabled / disabled / options and global options.
    skills: dict[str, Any] = field(default_factory=dict)
    # Cumulative token usage: input_tokens / output_tokens / cache_hit_tokens / total_tokens.
    usage: dict[str, int] = field(default_factory=empty_usage)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    approve: ApproveFn | None = None
    cancelled: CancelFn | None = None

    def __post_init__(self) -> None:
        self.session_id = str(self.session_id or "default")
        if self.workspace is not None:
            self.workspace = Path(self.workspace).expanduser().resolve()
        self.context = normalize_messages(self.context)
        self.rounds = _normalize_rounds(self.rounds)
        self.metadata = dict(self.metadata)
        self.tools = _capability_map(self.tools)
        self.skills = _capability_map(self.skills)
        self.usage = _coerce_usage(self.usage)
        # Backward compatibility: migrate the reserved keys in metadata to separate fields.
        self._absorb_reserved(self.metadata)

    def _absorb_reserved(self, values: Mapping[str, Any]) -> None:
        """Take the reserved keys (tools/skills/usage) out of ``values`` and write them into separate fields.

        ``values`` is modified in place (the reserved keys are removed). Tool/Skill config is written via
        a shallow merge, so both old data (a complete config) and partial updates land correctly.
        """
        tools = values.pop(TOOL_CONFIG_KEY, None)
        if tools is not None:
            merged = dict(self.tools)
            merged.update(_capability_map(tools))
            self.tools = merged
        skills = values.pop(SKILL_CONFIG_KEY, None)
        if skills is not None:
            merged = dict(self.skills)
            merged.update(_capability_map(skills))
            self.skills = merged
        usage = values.pop(USAGE_KEY, None)
        if usage is not None:
            self.usage = _coerce_usage(usage)

    # ------------------------------------------------------------------
    # Context representation
    # ------------------------------------------------------------------

    def messages(self) -> list[Message]:
        """Return a copy of the context representation; modifying the return value does not affect the Session."""
        return list(self.context)

    def append(self, messages: MessageInput | Iterable[MessageInput]) -> "Session":
        """Append messages (message objects, dicts, or collections of them).

        Appended to ``context``; if there is an in-progress round (opened by ``begin_round()``), the
        messages are also merged into that round. Use ``record_round()`` when this round's usage needs to
        be recorded separately.
        """
        items = normalize_messages(messages)
        self.context.extend(items)
        if self._open_round is not None:
            self._open_round.context.extend(items)
        return self.touch()

    def replace(self, messages: MessageInputs) -> "Session":
        """Replace the whole context representation (also clears the per-round records and the in-progress round, keeping the two consistent)."""
        self.context = normalize_messages(messages)
        self.rounds = []
        self._open_round = None
        return self.touch()

    def clear(self) -> "Session":
        """Clear the context representation and per-round records (keeping the workspace and metadata)."""
        self.context = []
        self.rounds = []
        self._open_round = None
        return self.touch()

    # ------------------------------------------------------------------
    # Round wrapping: one round = one complete agent-loop
    # Round = context (this round's messages: request + each reply + tool results) + usage (this round's aggregated usage)
    # ------------------------------------------------------------------

    @property
    def open_round(self) -> Round | None:
        """Return the in-progress round (opened by ``begin_round()``, before ``end_round()`` finalizes it)."""
        return self._open_round

    def begin_round(self, *, created_at: str | None = None) -> Round:
        """Start one complete conversation round (AgentEngine uses it to collect one agent-loop into one round).

        If there is already an in-progress round, return it directly (idempotent). Before ``end_round()``,
        the messages and usage written by ``append()`` / ``record_round()`` are merged into this round, so
        one round covers "one user request -> multiple LLM calls + tool execution -> final answer".
        """
        if self._open_round is None:
            round = Round(round=len(self.rounds) + 1)
            start = created_at or utc_now()
            usage = _coerce_round_usage(round.usage)
            usage["created_at"] = start
            usage["updated_at"] = start
            round.usage = usage
            self._open_round = round
        return self._open_round

    def end_round(self, *, updated_at: str | None = None) -> Round | None:
        """End the in-progress round (one complete conversation ends): finalize this round's messages and usage into ``rounds``.

        On end, it re-measures the context space occupied using all of this round's messages and refreshes
        this round's end time. Returns ``None`` when there is no in-progress round, or when this round has
        no messages yet (no empty round is produced).
        """
        round = self._open_round
        if round is None:
            return None
        self._open_round = None
        if not round.context:
            return None
        round.round = len(self.rounds) + 1
        self.rounds.append(self._refresh_round(round, updated_at=updated_at))
        self.touch()
        return round

    def record_round(
        self,
        messages: MessageInputs = (),
        usage: Mapping[str, Any] | None = None,
        *,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> "Session":
        """Write one round's messages and usage into the session.

        - When there is an in-progress round (``begin_round()``, e.g. the complete agent-loop AgentEngine
          is running), this round's messages and usage are **merged** into it (``created_at`` /
          ``updated_at`` follow that round);
        - Otherwise this round is **finalized immediately** as a standalone round (when using
          ``ContextMiddleware`` standalone, a single ``complete()`` / ``stream()`` is one round).

        ``messages`` is appended to ``context``, and ``usage``'s this-round tokens are also accumulated
        into ``session.usage``. Returns ``self`` for chaining.
        """
        items = normalize_messages(messages)
        delta = _usage_delta(usage)
        self._add_session_usage(delta)
        round = self._open_round
        if round is not None:
            self.context.extend(items)
            round.context.extend(items)
            self._merge_round_usage(round, delta)
            return self.touch()
        self.context.extend(items)
        self.rounds.append(
            Round(
                round=len(self.rounds) + 1,
                context=items,
                usage=self._round_usage(
                    items, delta=delta, created_at=created_at, updated_at=updated_at
                ),
            )
        )
        return self.touch()

    def _add_session_usage(self, delta: Mapping[str, int]) -> None:
        """Accumulate one call's token delta into the session-level ``usage``."""
        current = _coerce_usage(self.usage)
        for metric in USAGE_METRICS:
            current[metric] += delta[metric]
        self.usage = current

    def _merge_round_usage(self, round: Round, delta: Mapping[str, int]) -> None:
        """Merge one call's token delta into a round's usage and refresh the occupied space / end time."""
        usage = _coerce_round_usage(round.usage)
        for metric in USAGE_METRICS:
            usage[metric] += delta[metric]
        self._refresh_usage_space(usage, round.context)
        usage["updated_at"] = utc_now()
        round.usage = usage

    @staticmethod
    def _refresh_usage_space(usage: dict[str, Any], messages: MessageInputs) -> None:
        """Re-measure the context space occupied by a batch of messages and write it back to usage."""
        tokens, chars = measure_context(messages)
        usage[CONTEXT_TOKENS_KEY] = tokens
        usage[CONTEXT_CHARS_KEY] = chars

    def _refresh_round(self, round: Round, *, updated_at: str | None = None) -> Round:
        """Refresh usage (occupied space + end time) based on this round's current messages, then return the round."""
        usage = _coerce_round_usage(round.usage)
        self._refresh_usage_space(usage, round.context)
        usage["updated_at"] = updated_at or utc_now()
        round.usage = usage
        return round

    def _round_usage(
        self,
        messages: MessageInputs,
        *,
        delta: Mapping[str, int] | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        """Build one round's usage: token consumption + occupied context space + start/end time."""
        counters = _usage_delta(None) if delta is None else dict(delta)
        tokens, chars = measure_context(messages)
        return {
            **counters,
            CONTEXT_TOKENS_KEY: tokens,
            CONTEXT_CHARS_KEY: chars,
            "created_at": created_at or self.updated_at or utc_now(),
            "updated_at": updated_at or utc_now(),
        }

    def round_messages(self) -> list[Message]:
        """Expand the messages of all finalized ``Round`` objects by round (a copy; excludes the in-progress round)."""
        messages: list[Message] = []
        for round in self.rounds:
            messages.extend(round.context)
        return messages

    def round_payloads(self) -> list[dict[str, Any]]:
        """Serialize the context into a per-round ``messages`` list.

        - Finalized rounds (``self.rounds``) are output directly;
        - The in-progress round (opened by ``begin_round()``, not yet ``end_round()``) is output at its
          current progress (re-measuring the occupied space), ensuring that persisting mid-way through an
          agent-loop does not lose messages;
        - Any remaining messages not covered by a round are filled into one round.

        So no message is ever lost on persistence.
        """
        payloads = [round.to_dict() for round in self.rounds]
        covered = sum(len(item.context) for item in self.rounds)
        open_round = self._open_round
        if open_round is not None:
            covered += len(open_round.context)
            if open_round.context:
                payloads.append(self._refresh_round(open_round).to_dict())
        leftover = list(self.context[covered:])
        if leftover:
            payloads.append(
                Round(
                    round=len(payloads) + 1,
                    context=leftover,
                    usage=self._round_usage(leftover),
                ).to_dict()
            )
        return payloads

    # ------------------------------------------------------------------
    # Session info
    # ------------------------------------------------------------------

    def touch(self) -> "Session":
        """Refresh ``updated_at``."""
        self.updated_at = utc_now()
        return self

    def set_workspace(self, workspace: str | Path | None) -> "Session":
        """Set the session workspace; ``None`` restores "inherited at runtime"."""
        self.workspace = None if workspace is None else Path(workspace).expanduser().resolve()
        return self.touch()

    @property
    def effective_workspace(self) -> Path:
        """The workspace actually in effect; falls back to the current process working directory when not explicitly specified."""
        return self.workspace or Path.cwd()

    def update_metadata(self, metadata: Mapping[str, Any] | None = None, **values: Any) -> "Session":
        """Merge session metadata.

        Reserved keys (``tools`` / ``skills`` / ``usage``) are not left in ``metadata``; instead they are
        written to the corresponding separate fields (Tool/Skill config via a shallow merge), for
        compatibility with the old calling style.
        """
        merged = dict(metadata or {})
        merged.update(values)
        self._absorb_reserved(merged)
        self.metadata.update(merged)
        return self.touch()

    # ------------------------------------------------------------------
    # Tool execution environment (the capabilities of the former tools.RuntimeContext)
    # ------------------------------------------------------------------

    @classmethod
    def cwd(cls, session_id: str = "default") -> "Session":
        """Use the current process working directory as the session workspace."""
        return cls(session_id=session_id, workspace=Path.cwd())

    def log(self, message: str) -> None:
        """Call the ``metadata["log"]`` callback (the tool output channel)."""
        callback = self.metadata.get("log")
        if callable(callback):
            callback(message)

    def is_cancelled(self) -> bool:
        """When the ``cancelled`` callback is truthy, the session has requested cancellation."""
        return bool(self.cancelled and self.cancelled())

    # ------------------------------------------------------------------
    # Tool config (session.tools)
    # ------------------------------------------------------------------

    def tool_config(self) -> dict[str, Any]:
        """Return the ``session.tools`` tool config (a copy)."""
        return self._capability_config(TOOL_CONFIG_KEY)

    def registered_tools(self) -> list[str]:
        """Return the names of tools registered (available) for this session, in registration order."""
        return _normalize_names(self.tool_config().get(REGISTERED_KEY))

    def register_tools(self, tools: Any) -> "Session":
        """Record the tools registered for this session.

        ``tools`` may be ``Tool`` objects, tool names, or an iterable collection of them; re-registering
        does not record duplicates and does not change ``enabled`` / ``disabled`` / ``options``.
        """
        return self._register_capabilities(TOOL_CONFIG_KEY, tools)

    def unregister_tools(self, tools: Any) -> "Session":
        """Remove from this session's registered tools (accepts ``Tool`` objects or tool names)."""
        return self._unregister_capabilities(TOOL_CONFIG_KEY, tools)

    def clear_registered_tools(self) -> "Session":
        """Clear the registered tools list (keeping ``enabled`` / ``disabled`` / ``options``)."""
        return self._clear_registered(TOOL_CONFIG_KEY)

    def tool_options(self, tool_name: str | None = None) -> dict[str, Any]:
        """Return the tool options; when a tool name is passed, merge that tool's specific config.

        Config structure::

            session.tools = {
                "registered": ["read_file"],       # tools registered for this session
                "enabled": ["read_file"],          # allowlist (effective when non-empty)
                "disabled": ["bash"],              # denylist
                "timeout": 30,                     # global option
                "options": {"bash": {"timeout": 60}},  # per-tool option
            }
        """
        return self._capability_options(TOOL_CONFIG_KEY, tool_name)

    def tool_enabled(self, tool_name: str) -> bool:
        """Determine whether a tool is enabled according to the session tool config."""
        return self._capability_enabled(TOOL_CONFIG_KEY, tool_name)

    def configure_tools(
        self,
        *,
        registered: Iterable[str] | None = None,
        enabled: Iterable[str] | None = None,
        disabled: Iterable[str] | None = None,
        options: Mapping[str, Mapping[str, Any]] | None = None,
        **values: Any,
    ) -> "Session":
        """Write the session-level tool config (merged into ``session.tools``).

        ``registered`` overrides "which tools this session registered"; the other parameters keep their
        meaning.
        """
        return self._configure_capabilities(
            TOOL_CONFIG_KEY,
            registered=registered,
            enabled=enabled,
            disabled=disabled,
            options=options,
            **values,
        )

    # ------------------------------------------------------------------
    # Skill config (session.skills)
    # ------------------------------------------------------------------

    def skill_config(self) -> dict[str, Any]:
        """Return the ``session.skills`` Skill config (a copy)."""
        return self._capability_config(SKILL_CONFIG_KEY)

    def registered_skills(self) -> list[str]:
        """Return the names of Skills registered (available) for this session, in registration order."""
        return _normalize_names(self.skill_config().get(REGISTERED_KEY))

    def register_skills(self, skills: Any) -> "Session":
        """Record the Skills registered for this session.

        ``skills`` may be ``SkillMetadata``, Skill names, or an iterable collection of them;
        re-registering does not record duplicates.
        """
        return self._register_capabilities(SKILL_CONFIG_KEY, skills)

    def unregister_skills(self, skills: Any) -> "Session":
        """Remove from this session's registered Skills (accepts ``SkillMetadata`` or names)."""
        return self._unregister_capabilities(SKILL_CONFIG_KEY, skills)

    def clear_registered_skills(self) -> "Session":
        """Clear the registered Skills list (keeping ``enabled`` / ``disabled`` / ``options``)."""
        return self._clear_registered(SKILL_CONFIG_KEY)

    def skill_options(self, skill_name: str | None = None) -> dict[str, Any]:
        """Return the Skill options; when a name is passed, merge that Skill's specific config."""
        return self._capability_options(SKILL_CONFIG_KEY, skill_name)

    def skill_enabled(self, skill_name: str) -> bool:
        """Determine whether a Skill is enabled according to the session Skill config."""
        return self._capability_enabled(SKILL_CONFIG_KEY, skill_name)

    def configure_skills(
        self,
        *,
        registered: Iterable[str] | None = None,
        enabled: Iterable[str] | None = None,
        disabled: Iterable[str] | None = None,
        options: Mapping[str, Mapping[str, Any]] | None = None,
        **values: Any,
    ) -> "Session":
        """Write the session-level Skill config (merged into ``session.skills``)."""
        return self._configure_capabilities(
            SKILL_CONFIG_KEY,
            registered=registered,
            enabled=enabled,
            disabled=disabled,
            options=options,
            **values,
        )

    # ------------------------------------------------------------------
    # Token usage（session.usage）
    # ------------------------------------------------------------------

    def record_usage(self, usage: Mapping[str, Any] | None) -> "Session":
        """Accumulate one LLM call's token usage into this session (``session.usage``).

        ``input_tokens`` / ``output_tokens`` / ``cache_hit_tokens`` are accumulated per call, and
        ``total_tokens`` is accumulated as ``input + output`` (consistent with ``LLMClient``). Passing
        ``None`` or empty usage does not change the cumulative value.

        Only the session cumulative value is updated; use ``record_round()`` when it must also be recorded
        into a round (per-round usage).
        """
        self._add_session_usage(_usage_delta(usage))
        return self.touch()

    def reset_usage(self) -> "Session":
        """Zero this session's cumulative token usage (``session.usage``)."""
        self.usage = empty_usage()
        return self.touch()

    # ------------------------------------------------------------------
    # Registration overview
    # ------------------------------------------------------------------

    def registrations(self) -> dict[str, list[str]]:
        """Return the tools and Skills registered for this session: ``{"tools": [...], "skills": [...]}``."""
        return {"tools": self.registered_tools(), "skills": self.registered_skills()}

    # ------------------------------------------------------------------
    # Shared implementation for Tool/Skill config
    # ------------------------------------------------------------------

    def _capability_config(self, config_key: str) -> dict[str, Any]:
        """Read the Tool/Skill config field (a copy); treat a non-Mapping as an empty config."""
        return _capability_map(getattr(self, config_key, None))

    def _set_capability_config(self, config_key: str, config: Mapping[str, Any]) -> "Session":
        """Write the ``session.tools`` / ``session.skills`` field and refresh ``updated_at``."""
        setattr(self, config_key, dict(config))
        return self.touch()

    def _capability_enabled(self, config_key: str, name: str) -> bool:
        """Denylist first, then the allowlist (effective when non-empty), otherwise enabled by default."""
        config = self._capability_config(config_key)
        disabled = config.get("disabled")
        if isinstance(disabled, (list, tuple, set, frozenset)) and name in disabled:
            return False
        enabled = config.get("enabled")
        if isinstance(enabled, (list, tuple, set, frozenset)) and enabled:
            return name in enabled
        return True

    def _capability_options(self, config_key: str, name: str | None = None) -> dict[str, Any]:
        """Merge the global options with a specific tool's/Skill's options."""
        config = self._capability_config(config_key)
        options = {
            key: value
            for key, value in config.items()
            if key not in _RESERVED_CONFIG_KEYS
        }
        if name is not None:
            per_item = config.get("options")
            if isinstance(per_item, Mapping):
                specific = per_item.get(name)
                if isinstance(specific, Mapping):
                    options.update(specific)
        return options

    def _configure_capabilities(
        self,
        config_key: str,
        *,
        registered: Iterable[str] | None = None,
        enabled: Iterable[str] | None = None,
        disabled: Iterable[str] | None = None,
        options: Mapping[str, Mapping[str, Any]] | None = None,
        **values: Any,
    ) -> "Session":
        """Merge-write the register/enable/disable/options config into ``session.tools`` / ``session.skills``."""
        config = self._capability_config(config_key)
        if registered is not None:
            config[REGISTERED_KEY] = _normalize_names(registered)
        if enabled is not None:
            config["enabled"] = _normalize_names(enabled)
        if disabled is not None:
            config["disabled"] = _normalize_names(disabled)
        if options is not None:
            merged: dict[str, Any] = dict(config.get("options") or {})
            for name, value in options.items():
                specific = dict(merged.get(name) or {})
                specific.update(value or {})
                merged[str(name)] = specific
            config["options"] = merged
        config.update(values)
        return self._set_capability_config(config_key, config)

    def _register_capabilities(self, config_key: str, values: Any) -> "Session":
        """Append names into ``registered`` (order-preserving, de-duplicated)."""
        names = _normalize_names(values)
        if not names:
            return self
        config = self._capability_config(config_key)
        current = _normalize_names(config.get(REGISTERED_KEY))
        for name in names:
            if name not in current:
                current.append(name)
        config[REGISTERED_KEY] = current
        return self._set_capability_config(config_key, config)

    def _unregister_capabilities(self, config_key: str, values: Any) -> "Session":
        """Remove names from ``registered`` (order-preserving)."""
        names = set(_normalize_names(values))
        if not names:
            return self
        config = self._capability_config(config_key)
        config[REGISTERED_KEY] = [
            name for name in _normalize_names(config.get(REGISTERED_KEY)) if name not in names
        ]
        return self._set_capability_config(config_key, config)

    def _clear_registered(self, config_key: str) -> "Session":
        """Clear ``registered``, keeping the other keys in the same config."""
        config = self._capability_config(config_key)
        config[REGISTERED_KEY] = []
        return self._set_capability_config(config_key, config)

    def tool_context(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
        approve: ApproveFn | None = None,
        cancelled: CancelFn | None = None,
    ) -> "Session":
        """Return the session view for this tool execution.

        The view shares ``session_id``, workspace, Tool/Skill config, and callbacks with the source
        session, and merges the caller's ``metadata`` (such as the engine's metadata for this call) into
        the session metadata before handing it to the tools; the session metadata takes precedence, and
        the current ``session_id`` is always written. Reserved keys (``tools`` / ``skills`` / ``usage``)
        are no longer passed through ``metadata``, to avoid overwriting the session's own config.
        """
        merged = dict(metadata or {})
        merged.update(self.metadata)
        merged["session_id"] = self.session_id
        for key in RESERVED_KEYS:
            merged.pop(key, None)
        return replace(
            self,
            metadata=merged,
            approve=approve or self.approve,
            cancelled=cancelled or self.cancelled,
            context=self.context,
        )

    def runtime_context(self, **kwargs: Any) -> "Session":
        """Compatibility with the old naming: equivalent to ``tool_context()``."""
        return self.tool_context(**kwargs)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable session snapshot.

        ``tools`` / ``skills`` / ``usage`` are top-level fields of the session snapshot and are no longer
        embedded in ``metadata`` (version 3 format); ``messages`` is a **list of rounds** (version 4
        format), each item shaped like::

            {"round": 1, "context": [...], "usage": {...}}
        """
        return {
            "session_id": self.session_id,
            "workspace": str(self.workspace) if self.workspace is not None else None,
            "metadata": dict(self.metadata),
            "tools": dict(self.tools),
            "skills": dict(self.skills),
            "usage": dict(self.usage),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": self.round_payloads(),
        }

    @classmethod
    def from_dict(cls, session_id: str, payload: Any) -> "Session":
        """Restore a session from a ``to_dict()`` result.

        Also compatible with old formats:

        - version 1: ``payload`` is directly a message list, and ``workspace`` is ``None`` (inherits the
          tool registry's workspace at runtime);
        - version 2: ``tools`` / ``skills`` / ``usage`` are embedded in ``metadata`` and are automatically
          migrated to separate fields on read;
        - version 3: ``messages`` is a flat message list, split into rounds "starting at each user
          message" on read (old data has no per-round tokens, recorded as 0; occupied context space is
          still measured).
        """
        if isinstance(payload, Session):
            return payload if payload.session_id == session_id else replace(payload, session_id=session_id)
        if isinstance(payload, (list, tuple)):
            return cls(session_id=session_id, context=list(payload))
        if not isinstance(payload, dict):
            raise ValueError(f"session data must be an object or a message list: {type(payload).__name__}")
        workspace = payload.get("workspace")
        metadata = dict(payload.get("metadata") or {})
        # Top-level separate fields take precedence; old keys in metadata are left for __post_init__ to migrate.
        for key in RESERVED_KEYS:
            if payload.get(key) is not None:
                metadata.pop(key, None)
        created_at = payload.get("created_at") or utc_now()
        updated_at = payload.get("updated_at") or utc_now()
        context, rounds = _split_rounds(
            payload.get("messages") or [], created_at=created_at, updated_at=updated_at
        )
        return cls(
            session_id=str(payload.get("session_id") or session_id),
            workspace=None if workspace is None else Path(workspace),
            context=context,
            rounds=rounds,
            metadata=metadata,
            tools=payload.get(TOOL_CONFIG_KEY),
            skills=payload.get(SKILL_CONFIG_KEY),
            usage=payload.get(USAGE_KEY),
            created_at=created_at,
            updated_at=updated_at,
        )


__all__ = [
    "Session",
    "Round",
    "ApproveFn",
    "CancelFn",
    "TOOL_CONFIG_KEY",
    "SKILL_CONFIG_KEY",
    "REGISTERED_KEY",
    "USAGE_KEY",
    "USAGE_METRICS",
    "ROUND_INDEX_KEY",
    "ROUND_CONTEXT_KEY",
    "CONTEXT_TOKENS_KEY",
    "CONTEXT_CHARS_KEY",
    "ROUND_SPACE_METRICS",
    "ROUND_TIME_KEYS",
    "RESERVED_KEYS",
    "empty_usage",
    "estimate_tokens",
    "measure_context",
    "utc_now",
]

