"""Bounded summary extraction for the parent transcript (design §3.5, §7.6).

The parent only ever receives the child's final assistant text, truncated to
``max_summary_bytes`` at a UTF-8 boundary.  When truncation happens the full
text is written to the artifact store **first**, and the event references it
with ``summary_ref`` (I12).  The inline prefix keeps the parent model useful even
when it never reads the artifact.
"""

from __future__ import annotations

from typing import Any, Optional

EMPTY_SUMMARY = "(subagent completed without a textual summary)"


def _text_of(content: Any) -> str:
    """Flatten a string or canonical content-part list onto plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return "".join(parts)
    return "" if content is None else str(content)


def extract_summary(state) -> str:
    """Return the last non-empty assistant text, or :data:`EMPTY_SUMMARY`."""
    for message in reversed(getattr(state, "messages", [])):
        if message.get("role") != "assistant":
            continue
        text = _text_of(message.get("content"))
        if text.strip():
            return text
    return EMPTY_SUMMARY


def truncate_utf8(text: str, limit: int) -> tuple[str, bool]:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes without splitting a char."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", "ignore"), True


async def build_summary(
    text: str,
    *,
    limit: int,
    artifacts=None,
    session_id: Optional[str] = None,
) -> tuple[str, Optional[str], bool]:
    """Return ``(inline_summary, summary_ref, truncated)``.

    The artifact write happens before the caller publishes the terminal event;
    a store that is unavailable degrades to the inline prefix (``summary_ref``
    stays ``None``) rather than failing the whole task.
    """
    prefix, truncated = truncate_utf8(text, limit)
    if not truncated:
        return prefix, None, False
    if artifacts is None or not getattr(artifacts, "available", False):
        return prefix, None, True
    artifact = await artifacts.put(
        text.encode("utf-8"), mime="text/plain", session_id=session_id,
    )
    return prefix, artifact.ref, True


__all__ = [
    "EMPTY_SUMMARY",
    "build_summary",
    "extract_summary",
    "truncate_utf8",
]
