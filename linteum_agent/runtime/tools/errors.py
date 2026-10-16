"""Tool-layer errors and the tool subset of the Appendix-A error codes (§21–§24).

Mirrors :mod:`linteum_agent.bus.errors`: an error is **data** (P4).  The runtime never
lets a tool exception escape onto the bus — it converts either a specific
:class:`ToolError` or a catch-all into a ``tool.call.failed`` payload carrying a
``code``.  The codes are shared with :class:`linteum_agent.bus.errors.ErrorCode` so the
whole framework speaks one vocabulary (Appendix A).

Two distinctions the rest of the system depends on:

* :class:`UnknownTool` (LLM hallucinated a name — do **not** retry) versus
  :class:`ToolUnavailable` (plugin draining — a retry may succeed), §21;
* :class:`ValidationError` (``E_BAD_ARGS`` — the model produced bad arguments)
  versus :class:`ToolExecutionError` (``E_TOOL_ERROR`` — the tool itself broke).
"""

from __future__ import annotations

from typing import Optional

from ...bus.errors import ErrorCode
from ..errors import ErrorSource, error_info

#: Tool / plugin codes from Appendix A, resolved once for a single source of truth.
E_UNKNOWN_TOOL = ErrorCode.E_UNKNOWN_TOOL.value
E_PLUGIN_UNLOADING = ErrorCode.E_PLUGIN_UNLOADING.value
E_PLUGIN_UNAVAILABLE = ErrorCode.E_PLUGIN_UNAVAILABLE.value
E_PLUGIN_ERROR = ErrorCode.E_PLUGIN_ERROR.value
E_BAD_ARGS = ErrorCode.E_BAD_ARGS.value
E_TIMEOUT = ErrorCode.E_TIMEOUT.value
E_CANCELLED = ErrorCode.E_CANCELLED.value
E_DENIED = ErrorCode.E_DENIED.value
E_TOOL_ERROR = ErrorCode.E_TOOL_ERROR.value
E_SANDBOX = ErrorCode.E_SANDBOX.value
E_BUDGET = ErrorCode.E_BUDGET.value
E_SAFETY = ErrorCode.E_SAFETY.value


class ToolError(Exception):
    """Base class for every tool-layer failure.

    ``code`` / ``source`` / ``retryable`` / ``enters_history`` mirror Appendix A
    so a failure can be logged or turned into an event without losing the retry
    semantics.  :meth:`as_payload` emits ``code`` + ``error`` **and** a structured
    ``error_info`` that pins the failure to ``source="tool"`` and carries the
    raise location plus the original exception text.
    """

    code: str = E_TOOL_ERROR
    source: str = ErrorSource.TOOL.value
    retryable: bool = False
    enters_history: bool = True
    #: Whether a *safety policy* (not a tool bug) blocked this call.  When true the
    #: failure is published with code ``E_SAFETY``, ``error_info.source == "safety"``
    #: and ``safety: true`` so the LLM is told to stop rather than retry/reroute.
    safety: bool = False

    def __init__(
        self,
        message: str = "",
        *,
        cause: Optional[BaseException] = None,
        location: Optional[str] = None,
        **context: object,
    ) -> None:
        super().__init__(message or self.__class__.__name__)
        self.context = context
        self.cause = cause
        self.location = location

    def error_info(self, *, location: Optional[str] = None):
        """Structured detail (source=tool, or source=safety) for this failure."""
        cause = self.cause
        context = dict(self.context)
        if self.safety:
            context.setdefault("safety", True)
        return error_info(
            self.code,
            source=ErrorSource.SAFETY.value if self.safety else self.source,
            location=location if location is not None else self.location,
            message=str(self),
            exc=cause,
            # when the ToolError itself is the original failure, surface it
            exc_type=type(cause).__name__ if cause is not None else type(self).__name__,
            detail=None if cause is not None else str(self),
            retryable=self.retryable,
            enters_history=self.enters_history,
            context=context,
        )

    def as_payload(self, *, location: Optional[str] = None) -> dict:
        """Render as a JSON-serialisable ``tool.call.failed`` payload fragment."""
        return {
            "code": self.code,
            "error": str(self),
            "safety": self.safety,
            **self.context,
            "error_info": self.error_info(location=location).to_dict(),
        }


class SafetyViolation(ToolError):
    """A *safety policy* blocked the call — not a tool bug.

    Published with code ``E_SAFETY``, ``error_info.source == "safety"`` and
    ``safety: true``.  The agent renders the tool result to the LLM with an
    explicit "blocked by safety policy" marker so the model stops instead of
    retrying or rerouting around the guard.
    """

    code = E_SAFETY
    retryable = False
    enters_history = True
    safety = True


class UnknownTool(ToolError):
    """The requested tool is not registered — the LLM hallucinated the name (§21)."""

    code = E_UNKNOWN_TOOL


class ToolUnavailable(ToolError):
    """The tool exists but is currently unavailable (its plugin is draining, §21)."""

    code = E_PLUGIN_UNLOADING
    retryable = True


class DuplicateTool(ToolError):
    """Two specs claim the same name during one atomic ``register_many`` (§21)."""

    code = E_PLUGIN_ERROR
    enters_history = False


class ValidationError(ToolError):
    """JSON-Schema validation of the tool arguments failed (``E_BAD_ARGS``).

    Replaces the ``jsonschema.ValidationError`` the design document references,
    so the runtime has no third-party dependency (Checklist #1).
    """

    code = E_BAD_ARGS
    enters_history = True


class ToolExecutionError(ToolError):
    """The tool raised while running (its own bug, ``E_TOOL_ERROR``)."""

    code = E_TOOL_ERROR
    retryable = True


class SandboxError(SafetyViolation):
    """The sandbox could not start or isolate the worker (``E_SAFETY``).

    Unified with the safety vocabulary: a call that cannot be isolated safely is
    blocked rather than run unguarded.
    """

    code = E_SAFETY


class ToolCancelled(ToolError):
    """The tool call was cancelled by the user / session interrupt (§16.3)."""

    code = E_CANCELLED
    enters_history = False


class ApprovalDenied(ToolError):
    """A human rejected a side-effecting tool call (§24, ``E_DENIED``)."""

    code = E_DENIED


#: ``WorkspaceViolation.kind`` values that represent a *safety policy* denial
#: (the model tried to exceed its authority) as opposed to a business/usage error.
_POLICY_KINDS = frozenset({
    "invalid_path",
    "invalid_mode",
    "absolute_path",
    "escape",
    "hidden",
    "read_only",
    "too_large",
})


class WorkspaceViolation(ToolError):
    """A tool path escaped (or would escape) its workspace root (§21/§24).

    Raised by :meth:`linteum_agent.runtime.tools.workspace.Workspace.resolve` and by the file
    tools it guards.  The message and ``context`` **never** carry an absolute
    path — only the offending *relative* path — so no filesystem layout leaks
    into events, logs or the approval preview (§11.9).

    Classified by ``kind``: policy kinds (escape / absolute_path / hidden /
    read_only / too_large …) are safety failures (``E_SAFETY``); the remaining
    kinds (``not_found`` / ``io_error`` / ``is_directory`` / ``not_regular_file``)
    are ordinary tool errors and keep ``E_TOOL_ERROR``.
    """

    code = E_TOOL_ERROR          # business/usage default
    enters_history = True

    def __init__(self, message: str = "", *, kind: Optional[str] = None, **context: object) -> None:
        if kind is not None:
            context.setdefault("kind", kind)
        super().__init__(message, **context)
        self.kind = kind if kind is not None else context.get("kind")
        if self.kind in _POLICY_KINDS:
            self.safety = True
            self.code = E_SAFETY  # instance-level override


class WorkspaceUnavailable(ToolError):
    """The workspace root is missing or not accessible (§21/§24).

    Distinct from :class:`WorkspaceViolation`: the path the model asked for may
    be perfectly inside the workspace, but the root itself cannot be reached
    (host misconfiguration / unmounted volume).  Never exposes the root path.
    """

    code = E_TOOL_ERROR
