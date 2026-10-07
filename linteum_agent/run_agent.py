"""Agent run entry point: provides a uniform Agent access API for hosts such as CLI / TUI / Web.

This module collapses "Agent initialization + context storage + Tool/Skill registration + event
handling" into a single facade (``AgentRunner``). A host program (command line, TUI, Web service,
other tools) only needs to construct an ``AgentRunner`` once, then:

- Send user messages: ``send()`` / ``asend()`` / ``stream()`` / ``astream()``
- Subscribe to events for real-time rendering: ``on()`` / ``add_listener()`` (the event stream is
  independent of the host type)
- Read responses: ``response()`` -> ``AgentResponse`` (text / reasoning / tool calls / usage)
- Persist context: ``save_context()`` / ``load_context()`` / ``close()``
- List all sessions: ``list_sessions()`` / ``AgentRunner.list_sessions()`` -> ``SessionInfo`` summary

The session identifier defaults to a UUID (``uuid4``), and the same context is saved to both the
**user home directory** (``~``) and the **workspace** at ``.agent/sessions/<session_id>.json``
(the two files are copies of each other; the user-level one is ``~/.agent/sessions`` and is no
longer written into the project repository / install directory).

Session lifecycle (construction **actively searches**, loading is **explicit**):

- Constructing an ``AgentRunner`` **actively searches** for a session: first an explicit
  ``session_id`` / ``context_path``, otherwise it searches existing contexts in the workspace and the
  user home directory according to ``session_priority`` (**workspace first by default**); on a hit it
  sets ``session_id`` to that session and records ``found_context_path``; only on a miss does it use a
  new ``uuid4``;
- When there is nothing in the workspace and it falls back to the user home directory, it also
  compares the ``workspace`` recorded in the JSON: one that does not match the current workspace is
  **not** the context being searched for (the user level is shared by multiple workspaces) and is
  treated as a miss (see ``context_matches_workspace()``);
- But construction **does not load content or create files**: ``loaded_context_paths`` is empty and
  ``get_context()`` is empty until ``load_session()`` (or ``load_context(path)``) is explicitly
  called;
- When an existing session was found but not explicitly loaded, ``send()`` / ``stream()`` /
  ``save_context()`` error out first (``has_unloaded_session``) to avoid overwriting existing content
  with an empty history;
- To start a new conversation: explicitly call ``create_context()`` -- when ``session_id`` is not
  specified it generates **a new ``uuid4`` every time**, so it never overwrites / resets the context
  of other sessions;
- Persisting: explicitly ``save_context()``, or ``close()`` (only when this round really changed
  something).


Composition (responsibilities of each layer): ::

    config.yaml  -> ConfigStore -> LLMConfig -> LLMClient
    Session(JSON) -> ContextStore -> Session -> ContextMiddleware
    ToolRegistry  + SkillRegistry -> AgentEngine
    AgentEngine -> EventBus -> ResponseCollector -> AgentResponse
    create_approve_callback() -> session.approve -> ToolRegistry (tools requiring approval such as bash)

Event stream (``stream()`` mode) ::

    agent_start
      -> message_start(user)
      -> turn_start
      -> message_update(text_delta / reasoning_delta) ...
      -> tool_execution_start -> tool_execution_update -> tool_execution_end
      -> turn_end
      -> turn_start
      -> message_update ... -> message_start -> message_end -> turn_end
      -> AgentStreamChunk(done=True)

``run()`` mode has no ``message_update`` deltas; the final text is carried by the ``message_end`` /
``turn_end`` / ``agent_end`` events. ``ResponseCollector`` produces the same ``AgentResponse`` for
both modes, so hosts such as TUI/Web do not need to distinguish synchronous / streaming themselves
(see ``/workspace/test/test_run/README.md``).

This module **does not include CLI parsing**: command line, TUI, and Web should all be host programs
that call ``build_agent()`` / ``AgentRunner`` and decide input/output themselves.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, Iterator, Mapping, TextIO

# ----------------------------------------------------------------------
# Support two import styles:
# 1) ``from linteum_agent.run_agent import ...`` (intra-package relative import);
# 2) ``import run_agent`` after adding ``/workspace/linteum_agent`` to sys.path.
# The skills package uses ``from ..tools import ...`` internally, so the latter must add the
# project root to sys.path and then import absolutely as ``linteum_agent.*`` to avoid
# "attempted relative import".
# ----------------------------------------------------------------------
try:  # pragma: no cover - depends on how it is invoked
    from .agent.engine import AgentEngine, AgentResult, AgentStreamChunk
    from .config import ConfigStore, LLMConfig
    from .context.middleware import ContextMiddleware
    from .context.session import ApproveFn, Session
    from .context.store import ContextStore
    from .evnts import AgentEvent, AgentEventListener, EventBus
    from .messages.agent_message import (
        AssistantMessage,
        MessageInput,
        MessageInputs,
        SystemPrompt,
        UserPrompt,
        normalize_messages,
    )
    from .model.client import LLMClient
    from .prompt import build_system_prompt as _build_default_system_prompt
    from .skills import SkillRegistry
    from .tools import Tool, ToolRegistry, create_builtin_tools
except ImportError:  # importing run_agent directly: add the project root, then use absolute imports
    _SCRIPT_ROOT = Path(__file__).resolve().parents[1]
    if str(_SCRIPT_ROOT) not in sys.path:
        sys.path.insert(0, str(_SCRIPT_ROOT))

    from linteum_agent.agent.engine import AgentEngine, AgentResult, AgentStreamChunk
    from linteum_agent.config import ConfigStore, LLMConfig
    from linteum_agent.context.middleware import ContextMiddleware
    from linteum_agent.context.session import ApproveFn, Session
    from linteum_agent.context.store import ContextStore
    from linteum_agent.evnts import AgentEvent, AgentEventListener, EventBus
    from linteum_agent.messages.agent_message import (
        AssistantMessage,
        MessageInput,
        MessageInputs,
        SystemPrompt,
        UserPrompt,
        normalize_messages,
    )
    from linteum_agent.model.client import LLMClient
    from linteum_agent.prompt import build_system_prompt as _build_default_system_prompt
    from linteum_agent.skills import SkillRegistry
    from linteum_agent.tools import Tool, ToolRegistry, create_builtin_tools


__all__ = [
    "AgentResponse",
    "ResponseCollector",
    "AgentRunner",
    "build_agent",
    "run_agent",
    "create_approve_callback",
    "confirm_tool_call",
    "describe_tool_risk",
    "TOOL_RISK_NOTES",
    "new_session_id",
    "session_context_paths",
    "session_search_dirs",
    "context_json_workspace",
    "context_matches_workspace",
    "find_context_json",
    "find_context_jsons",
    "create_context_json",
    "SessionInfo",
    "list_sessions",
    "SESSION_SCOPE_WORKSPACE",
    "SESSION_SCOPE_PROJECT",
    "SESSION_SCOPE_CONTEXT",
    "SESSION_PRIORITY_WORKSPACE",
    "SESSION_PRIORITY_PROJECT",
    "DEFAULT_SESSION_PRIORITY",
]

# Placeholder label used when no session is specified; real sessions use ``new_session_id()`` (uuid4) by default.
DEFAULT_SESSION_ID = "default"
DEFAULT_MAX_TOOL_ROUNDS = 8

# Default storage directory for session context: one copy in the user home directory (``~``) and one
# in the workspace (``<root>/.agent/sessions``).
DEFAULT_SESSION_DIRNAME = Path(".agent") / "sessions"

# Session lookup priority: the same session has a copy in each location; this decides which wins.
# The default ``workspace`` = **workspace first**, i.e. look at ``.agent/sessions`` under the current
# workspace first, then the user home directory (``~/.agent/sessions``); pass
# ``SESSION_PRIORITY_PROJECT`` explicitly to give the user level priority.
SESSION_PRIORITY_WORKSPACE = "workspace"
SESSION_PRIORITY_PROJECT = "project"
DEFAULT_SESSION_PRIORITY = SESSION_PRIORITY_WORKSPACE

# Default Skill search directories: system level under the user directory, project level under the
# workspace ``.agent/skills``.
# When a directory does not exist, SkillRegistry.discover() skips it automatically.
_SYSTEM_SKILL_DIR = Path.home() / ".agent" / "skills"
_PROJECT_SKILL_DIRNAME = Path(".agent") / "skills"

# Event type constants: avoid callers hard-coding strings.
EVENT_AGENT_START = "agent_start"
EVENT_AGENT_END = "agent_end"
EVENT_TURN_START = "turn_start"
EVENT_TURN_END = "turn_end"
EVENT_MESSAGE_START = "message_start"
EVENT_MESSAGE_UPDATE = "message_update"
EVENT_MESSAGE_END = "message_end"
EVENT_TOOL_START = "tool_execution_start"
EVENT_TOOL_UPDATE = "tool_execution_update"
EVENT_TOOL_END = "tool_execution_end"

ALL_EVENTS = (
    EVENT_AGENT_START,
    EVENT_AGENT_END,
    EVENT_TURN_START,
    EVENT_TURN_END,
    EVENT_MESSAGE_START,
    EVENT_MESSAGE_UPDATE,
    EVENT_MESSAGE_END,
    EVENT_TOOL_START,
    EVENT_TOOL_UPDATE,
    EVENT_TOOL_END,
)


# ----------------------------------------------------------------------
# Tool approval (approve): risk notice + user confirmation
# ----------------------------------------------------------------------
# Risk notes for high-risk tools; ``create_approve_callback()`` looks them up by tool name when
# showing the risk notice.
TOOL_RISK_NOTES: dict[str, str] = {
    "bash": "Execute arbitrary Bash commands in the workspace: can read/write / delete files, access the network, and start subprocesses.",
    "write_file": "Write to (overwrite) files in the workspace.",
}

# Answers treated as "approval" (case-insensitive); any other input is treated as a rejection.
_APPROVE_ANSWERS = ("y", "yes")


def _stdin_is_interactive() -> bool:
    """Whether the current process's stdin is interactive (not a pipe / redirect / closed stream)."""
    try:
        return bool(sys.stdin) and bool(sys.stdin.isatty())
    except (AttributeError, ValueError):  # pragma: no cover - closed stdin
        return False


def describe_tool_risk(tool_name: str, arguments: Mapping[str, Any] | None = None) -> str:
    """Render the risk notice text for a pending tool call (tool name + risk note + arguments).

    For tools not registered in ``TOOL_RISK_NOTES`` (for example a custom tool that a host
    registers via ``extra_tools`` with ``requires_approval=True``) a generic notice is given.
    """
    note = TOOL_RISK_NOTES.get(
        tool_name, "This tool is marked as requiring approval; please confirm the arguments are safe."
    )
    lines = [f"Tool {tool_name} requested execution.", f"Risk notice: {note}"]
    if arguments:
        try:
            rendered = json.dumps(dict(arguments), ensure_ascii=False, default=str)
        except (TypeError, ValueError):  # pragma: no cover - fallback
            rendered = str(arguments)
        lines.append(f"Arguments: {rendered}")
    return "\n".join(lines)


def confirm_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    input_fn: Callable[[str], str] | None = None,
    stream: TextIO | None = None,
    default: bool = False,
) -> bool:
    """Show the tool call's risk notice to the user and wait for confirmation; return whether approved.

    - ``input_fn=None`` (default): use the built-in ``input()``; when ``stdin`` is not a terminal
      (pipe, CI, Web process, etc.) interaction is impossible, so ``default`` is returned directly to
      avoid blocking the caller;
    - Only ``y`` / ``yes`` (case-insensitive) count as approval; any other input is treated as a
      rejection;
    - Interactive exceptions such as Ctrl+C / Ctrl+D also return ``default``;
    - ``stream`` is the notice output stream, defaulting to ``sys.stderr`` (so it does not pollute
      normal output such as the streamed body).
    """
    output = stream if stream is not None else sys.stderr
    print(describe_tool_risk(tool_name, arguments), file=output, flush=True)

    prompt_fn = input_fn
    if prompt_fn is None:
        if not _stdin_is_interactive():
            verdict = "allow" if default else "deny"
            print(
                f"Cannot interactively confirm in the current environment; applying the default policy ({verdict}): {tool_name}",
                file=output,
                flush=True,
            )
            return bool(default)
        prompt_fn = input

    try:
        answer = prompt_fn(f"Allow execution of {tool_name}? [y/N] ")
    except (EOFError, KeyboardInterrupt, OSError):
        print(f"\nNo valid confirmation received; treating as denied: {tool_name}", file=output, flush=True)
        return bool(default)
    return str(answer).strip().lower() in _APPROVE_ANSWERS


def create_approve_callback(
    *,
    auto_approve: bool = False,
    input_fn: Callable[[str], str] | None = None,
    stream: TextIO | None = None,
    default: bool = False,
) -> ApproveFn:
    """Create the tool approval callback handed to ``ToolRegistry`` (i.e. ``session.approve``).

    The callback signature matches ``Session.approve``: ``(tool_name, arguments) -> bool``.

    - ``auto_approve=True``: return ``True`` directly -- no risk notice, no user prompt (approve
      everything);
    - ``auto_approve=False`` (default): print the risk notice and ask the user to confirm (see
      ``confirm_tool_call()``);
    - ``input_fn`` / ``stream`` / ``default`` are passed through to ``confirm_tool_call()`` so hosts
      such as TUI / Web can take over the interaction (by default ``input()`` + ``sys.stderr`` are
      used, and it denies when interaction is impossible).
    """
    def approve(tool_name: str, arguments: dict[str, Any]) -> bool:
        if auto_approve:
            return True
        return confirm_tool_call(
            tool_name, arguments, input_fn=input_fn, stream=stream, default=default
        )

    return approve


# ----------------------------------------------------------------------
# Session identity and context paths
# ----------------------------------------------------------------------


def new_session_id() -> str:
    """Generate a new session ID (the canonical string form of ``uuid4``)."""
    return str(uuid.uuid4())


def default_session_root() -> Path:
    """The default root directory for session storage: the **user home directory** (``~``).

    Under it, ``.agent/sessions`` (i.e. ``~/.agent/sessions``) is the user-level shared copy; the
    other copy is under the workspace ``<workspace>/.agent/sessions``. The two are copies of each
    other, so sessions are no longer written into the project repository / install directory.
    """
    return Path.home()


def session_context_paths(
    session_id: str,
    workspace: str | Path,
    *,
    project_dir: str | Path | None = None,
    dirname: str | Path = DEFAULT_SESSION_DIRNAME,
) -> list[Path]:
    """Return the two context storage paths for a session: user home directory + workspace.

    Paths look like ``<root>/<dirname>/<session_id>.json`` (the user level being
    ``~/.agent/sessions``). When the user home directory and the workspace are exactly the same,
    they are de-duplicated, so only one path is returned.
    """
    root = Path(project_dir).expanduser() if project_dir is not None else default_session_root()
    roots = [root, Path(workspace).expanduser()]
    paths: list[Path] = []
    for item in roots:
        candidate = item / dirname / f"{session_id}.json"
        if candidate not in paths:
            paths.append(candidate)
    return paths


def _normalize_session_priority(priority: str | None) -> str:
    """Normalize the priority to ``workspace`` / ``project``; raise ``ValueError`` on invalid values."""
    if priority is None:
        return DEFAULT_SESSION_PRIORITY
    value = str(priority).strip().lower()
    if value in (SESSION_PRIORITY_WORKSPACE, SESSION_PRIORITY_PROJECT):
        return value
    raise ValueError(
        f"priority must be {SESSION_PRIORITY_WORKSPACE!r} or "
        f"{SESSION_PRIORITY_PROJECT!r}, got {priority!r}"
    )


def session_search_dirs(
    workspace: str | Path,
    *,
    project_dir: str | Path | None = None,
    dirname: str | Path = DEFAULT_SESSION_DIRNAME,
    priority: str = DEFAULT_SESSION_PRIORITY,
) -> list[Path]:
    """Return the session search directories, **ordered by priority** (default: workspace first, user home directory second).

    When the two root directories are exactly the same they are de-duplicated automatically, so
    usually only one directory is returned.
    """
    root = Path(project_dir).expanduser() if project_dir is not None else default_session_root()
    workspace_dir = Path(workspace).expanduser() / dirname
    project_leaf = root / dirname
    ordered = (
        [workspace_dir, project_leaf]
        if _normalize_session_priority(priority) == SESSION_PRIORITY_WORKSPACE
        else [project_leaf, workspace_dir]
    )

    dirs: list[Path] = []
    for item in ordered:
        if item not in dirs:
            dirs.append(item)
    return dirs


def context_json_workspace(
    path: str | Path,
    *,
    session_id: str | None = None,
) -> Path | None:
    """Read the session workspace recorded in a context JSON (read-only; never creates any file).

    - When ``session_id`` is given, read that session's record; otherwise read only when the file
      contains exactly one session;
    - Returning ``None`` means "undetermined": the file cannot be read / is not this module's
      ``ContextStore`` format / the session has not recorded a workspace yet (version 1 old format,
      inherits the current workspace at runtime).
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, dict):
        return None

    entry: Any = None
    if session_id is not None:
        entry = sessions.get(session_id)
    elif len(sessions) == 1:
        entry = next(iter(sessions.values()))

    recorded = entry.get("workspace") if isinstance(entry, dict) else None
    if not recorded:
        return None
    return Path(str(recorded)).expanduser().resolve()


def context_matches_workspace(
    path: str | Path,
    workspace: str | Path,
    *,
    session_id: str | None = None,
) -> bool:
    """Determine whether the session workspace recorded in a context JSON matches ``workspace``.

    - If no workspace is recorded in the file (old format / unreadable): by ``Session`` semantics it
      "inherits the current workspace at runtime", so ``True`` is returned;
    - If a workspace is recorded: compare it with ``workspace`` after resolving both to absolute
      paths; return ``True`` only when they match.

    Session files under the user home directory are **shared by multiple workspaces** (the same
    session_id only writes ``<id>.json``), so when falling back to the user-level lookup this is used
    to confirm "this context really belongs to the current workspace".
    """
    return _same_workspace(context_json_workspace(path, session_id=session_id), workspace)


def find_context_jsons(
    workspace: str | Path,
    *,
    project_dir: str | Path | None = None,
    dirname: str | Path = DEFAULT_SESSION_DIRNAME,
    session_id: str | None = None,
    priority: str = DEFAULT_SESSION_PRIORITY,
) -> list[Path]:
    """Find **existing** context JSONs (read-only; never creates files or directories).

    Result order = search priority (``priority``, default ``workspace`` = workspace first):

    - With ``session_id``: search only that session's two copies, returning them by priority
      (returning as many as exist);
    - Without it: first list the ``*.json`` files in the higher-priority directory (within the
      directory, newest to oldest by mtime), then those of the other directory, de-duplicating by
      path.

    Files under the workspace directory (``<workspace>/<dirname>``) naturally belong to the current
    workspace and are adopted directly; those under the **user home directory** are shared by multiple
    workspaces, so the ``workspace`` recorded in the file must be compared
    (``context_matches_workspace()``): one that does not match the current ``workspace`` is **not**
    the context being searched for and is skipped -- when there is nothing in the workspace and the
    user level does not match either, the result is an empty list.

    Note the difference from ``session_context_paths()``: the latter is the **write/enumeration
    order** (user home directory -> workspace), while this function is the **search order** (default
    workspace -> user home directory).
    """
    directories = session_search_dirs(
        workspace, project_dir=project_dir, dirname=dirname, priority=priority
    )
    workspace_dir = Path(workspace).expanduser() / dirname

    def usable(candidate: Path) -> bool:
        """Files in the workspace directory are adopted directly; user-level files must first be compared against the recorded workspace."""
        if candidate.parent == workspace_dir:
            return True
        return context_matches_workspace(candidate, workspace, session_id=session_id)

    if session_id is not None:
        found: list[Path] = []
        for directory in directories:
            candidate = directory / f"{session_id}.json"
            if candidate.is_file() and candidate not in found and usable(candidate):
                found.append(candidate)
        return found

    ordered: list[Path] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        files = [path for path in directory.glob("*.json") if path.is_file()]
        files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        ordered.extend(path for path in files if path not in ordered and usable(path))
    return ordered


def find_context_json(
    workspace: str | Path,
    *,
    project_dir: str | Path | None = None,
    dirname: str | Path = DEFAULT_SESSION_DIRNAME,
    session_id: str | None = None,
    priority: str = DEFAULT_SESSION_PRIORITY,
) -> Path | None:
    """Find a single context JSON; return the **highest-priority** one when found, otherwise ``None``.

    This is the entry point for "finding a workspace context": default **workspace first**
    (``priority="workspace"``), falling back to the user home directory only when the workspace has
    none; within the same directory the most recently modified one is taken.

    When falling back to the user home directory it also compares the ``workspace`` recorded in the
    file: one that does not match the current ``workspace`` is **not** the context being searched for
    (the user level is shared by multiple workspaces) and ``None`` is returned directly -- rather than
    treating another workspace's history as this context, let the caller create a new session; when the
    old format has no recorded workspace it is treated as "inherited at runtime" and still adopted (see
    ``context_matches_workspace()``).

    Read-only lookup; never creates any file. When a session needs to be continued, the caller must
    decide **explicitly**.
    """
    files = find_context_jsons(
        workspace,
        project_dir=project_dir,
        dirname=dirname,
        session_id=session_id,
        priority=priority,
    )
    return files[0] if files else None


def create_context_json(
    session_id: str,
    workspace: str | Path,
    *,
    path: str | Path | None = None,
    project_dir: str | Path | None = None,
    dirname: str | Path = DEFAULT_SESSION_DIRNAME,
    metadata: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> list[Path]:
    """**Actively** create context JSON (two copies, or only the one specified by ``path``).

    This is the entry point for "creating context"; only calling it (or
    ``AgentRunner.create_context()``) creates a file when no session file exists. The construction of
    ``AgentRunner`` itself never creates any JSON.

    - If the file already exists and ``overwrite=False``: keep the original content, only return its
      path;
    - ``overwrite=True``: overwrite with a brand-new session;
    - Return the list of file paths that finally exist (construction order: user home directory ->
      workspace).
    """
    resolved_workspace = Path(workspace).expanduser()
    targets = (
        [Path(path).expanduser()]
        if path is not None
        else session_context_paths(
            session_id, resolved_workspace, project_dir=project_dir, dirname=dirname
        )
    )

    store = ContextStore(workspace=resolved_workspace)
    store.ensure_session(session_id, workspace=resolved_workspace, metadata=metadata)

    created: list[Path] = []
    for target in targets:
        if target.is_file() and not overwrite:
            created.append(target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        store.save_session(target, session_id)
        created.append(target)
    return created


# ----------------------------------------------------------------------
# Session inventory: list all sessions (workspace + user home directory, read-only)
# ----------------------------------------------------------------------

# Values of ``SessionInfo.scope``: the location / source of the session file.
SESSION_SCOPE_WORKSPACE = "workspace"   # <workspace>/.agent/sessions/<id>.json
SESSION_SCOPE_PROJECT = "project"       # user level ~/.agent/sessions/<id>.json (historical name for the user home directory copy)
SESSION_SCOPE_CONTEXT = "context"       # a single file specified by an explicit context_path


@dataclass
class SessionInfo:
    """A **read-only summary** of a session (id / file path / location / time / size / usage).

    Produced by ``list_sessions()`` (module level) and ``AgentRunner.list_sessions()`` for hosts to
    list "all sessions"; it **does not contain the full message history** -- when history is needed,
    load it with ``load_session()`` and read it with ``get_context()``. ``to_dict()`` / ``to_json()``
    make it easy to return directly to Web / TUI.

    Fields:

    - ``session_id``: session ID (the key under ``sessions`` in the file);
    - ``path``: the context JSON file this session lives in;
    - ``scope``: file location -- ``"workspace"`` / ``"project"`` / ``"context"`` (an explicit
      ``context_path``);
    - ``workspace``: the workspace recorded in the file; ``None`` = "inherits the current workspace
      at runtime";
    - ``matches_workspace``: whether the recorded workspace matches the query ``workspace``
      (``None`` counts as a match, same convention as ``context_matches_workspace()``);
    - ``created_at`` / ``updated_at``: session creation / update time (UTC ISO8601);
    - ``message_count`` / ``round_count``: number of messages / conversation rounds;
    - ``usage``: cumulative token usage of the session;
    - ``metadata`` / ``tools`` / ``skills``: session metadata and Tool / Skill config;
    - ``size`` / ``mtime``: file size in bytes and last modification time.
    """

    session_id: str
    path: Path
    scope: str
    workspace: Path | None = None
    matches_workspace: bool = True
    created_at: str = ""
    updated_at: str = ""
    message_count: int = 0
    round_count: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, Any] = field(default_factory=dict)
    skills: dict[str, Any] = field(default_factory=dict)
    size: int = 0
    mtime: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to a directly JSON-serializable dict (``path`` / ``workspace`` become strings)."""
        return {
            "session_id": self.session_id,
            "path": str(self.path),
            "scope": self.scope,
            "workspace": str(self.workspace) if self.workspace is not None else None,
            "matches_workspace": self.matches_workspace,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
            "round_count": self.round_count,
            "usage": dict(self.usage),
            "metadata": dict(self.metadata),
            "tools": dict(self.tools),
            "skills": dict(self.skills),
            "size": self.size,
            "mtime": self.mtime,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)


def _same_workspace(recorded: Path | None, workspace: str | Path) -> bool:
    """Pure in-memory version of ``context_matches_workspace()`` (``recorded`` already parsed by ``Session``).

    A record of ``None`` (old format / unspecified) -> return ``True`` per "inherits the current
    workspace at runtime".
    """
    if recorded is None:
        return True
    try:
        current = Path(workspace).expanduser().resolve()
    except OSError:  # pragma: no cover - fallback (e.g. a broken symlink)
        current = Path(workspace).expanduser()
    return Path(recorded) == current


def _file_stat(path: Path) -> tuple[int, float]:
    """Return ``(size, mtime)``; return ``(0, 0.0)`` when the file cannot be read (never raises)."""
    try:
        stat = path.stat()
    except OSError:
        return 0, 0.0
    return stat.st_size, stat.st_mtime


def _sessions_in_file(path: str | Path) -> list[Session]:
    """Read all ``Session`` objects in one context file (read-only; returns an empty list on a bad file / invalid data)."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, dict):
        return []

    parsed: list[Session] = []
    for session_id, value in sessions.items():
        if isinstance(value, dict) and "role" in value:
            continue  # a single message item, not a session snapshot
        try:
            parsed.append(Session.from_dict(str(session_id), value))
        except (TypeError, ValueError):
            continue
    return parsed


def _session_info(
    session: Session,
    path: Path,
    scope: str,
    *,
    workspace: str | Path,
) -> SessionInfo:
    """Assemble a ``Session`` and its file into a ``SessionInfo``."""
    size, mtime = _file_stat(path)
    return SessionInfo(
        session_id=session.session_id,
        path=path,
        scope=scope,
        workspace=session.workspace,
        matches_workspace=_same_workspace(session.workspace, workspace),
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=len(session.messages()),
        round_count=len(session.rounds),
        usage=dict(session.usage),
        metadata=dict(session.metadata),
        tools=dict(session.tools),
        skills=dict(session.skills),
        size=size,
        mtime=mtime,
    )


def _sort_infos(infos: list[SessionInfo]) -> list[SessionInfo]:
    """Sort by ``updated_at`` newest to oldest (then ``session_id``); return the same list."""
    infos.sort(key=lambda info: (info.updated_at, info.session_id), reverse=True)
    return infos


def list_sessions(
    workspace: str | Path,
    *,
    project_dir: str | Path | None = None,
    dirname: str | Path = DEFAULT_SESSION_DIRNAME,
    include_project: bool = True,
    only_current_workspace: bool = False,
    dedupe: bool = True,
    priority: str = DEFAULT_SESSION_PRIORITY,
) -> list[SessionInfo]:
    """List a read-only summary of **all sessions** (does not load content, does not create any file).

    Scan the ``*.json`` files in the session directories ordered by ``priority`` (default workspace
    first, user home directory second), assembling every session in every file into a ``SessionInfo``:

    - ``include_project=False``: list only the workspace directory; ``True`` (default) additionally
      lists the user home directory;
    - ``only_current_workspace=True``: keep only sessions whose ``matches_workspace`` is true (the
      same convention as the user-level fallback comparison of ``find_context()``, i.e. sessions from
      other workspaces are not counted);
    - ``dedupe=True`` (default): when the same ``session_id`` has a copy in both the workspace / user
      home directory, keep only the higher-priority one (the workspace copy by default, see
      ``scope``); with ``False`` both are returned;
    - Sorting: ``updated_at`` newest to oldest, then ``session_id``;
    - Fault tolerance: corrupted file / not ``ContextStore`` format / invalid single session data ->
      skip that entry, never raise;
    - When "just the file paths" are needed (without parsing content), use ``find_context_jsons()``;
    - ``SessionInfo.to_dict()`` / ``to_json()`` can be handed directly to Web / TUI for rendering.

    Read-only operation; never creates directories or files.
    """
    workspace_dir = Path(workspace).expanduser() / dirname
    directories = session_search_dirs(
        workspace, project_dir=project_dir, dirname=dirname, priority=priority
    )
    if not include_project:
        directories = [directory for directory in directories if directory == workspace_dir]

    infos: list[SessionInfo] = []
    seen: set[str] = set()
    for directory in directories:
        if not directory.is_dir():
            continue
        scope = (
            SESSION_SCOPE_WORKSPACE if directory == workspace_dir else SESSION_SCOPE_PROJECT
        )
        files = [path for path in directory.glob("*.json") if path.is_file()]
        files.sort(key=lambda path: _file_stat(path)[1], reverse=True)
        for path in files:
            for session in _sessions_in_file(path):
                if dedupe and session.session_id in seen:
                    continue
                info = _session_info(session, path, scope, workspace=workspace)
                if only_current_workspace and not info.matches_workspace:
                    continue
                infos.append(info)
                seen.add(session.session_id)
    return _sort_infos(infos)


# ----------------------------------------------------------------------
# Event aggregation: turn AgentEvent back into "a response the user can see and keep handling"
# ----------------------------------------------------------------------


@dataclass
class AgentResponse:
    """The Agent response that ``ResponseCollector`` aggregates from the event stream.

    ``text`` / ``reasoning`` take the text and reasoning of "the last completed turn" (that is, the
    final answer); use ``streamed_text`` when the full streamed text is needed. ``tool_calls`` /
    ``tool_results`` aggregate all tool rounds within this request.
    """

    text: str = ""
    reasoning: str = ""
    streamed_text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    session_id: str = DEFAULT_SESSION_ID
    events: list[AgentEvent] = field(default_factory=list)
    result: AgentResult | None = None

    @property
    def ok(self) -> bool:
        """Whether a structured ``AgentResult`` was obtained (``True`` when events complete normally)."""
        return self.result is not None

    def to_dict(self, *, include_events: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "text": self.text,
            "reasoning": self.reasoning,
            "tool_calls": self.tool_calls,
            "tool_results": self.tool_results,
            "usage": self.usage,
        }
        if include_events:
            payload["events"] = [dict(event) for event in self.events]
        return payload

    def to_json(self, *, include_events: bool = False, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(include_events=include_events),
                          ensure_ascii=False, indent=indent, default=str)


def _assistant_from(message: Any) -> AssistantMessage | None:
    return message if isinstance(message, AssistantMessage) else None


class ResponseCollector(AgentEventListener):
    """Subscribe to all ``EventBus`` events and aggregate them into an ``AgentResponse``.

    Why a synchronous callback: ``AgentEngine.run()`` / ``stream()`` dispatch events through
    ``EventBus.emit_sync()``, so synchronous callbacks run immediately; an asynchronous callback would
    merely be ``create_task``-ed (not awaited) when there is a running event loop, making the
    aggregation result unreliable. ``EventBus.emit()`` (the async engine) also supports synchronous
    callbacks, so this listener works for all four entry points.

    Event-to-response mapping:

    ============================  =================================================
    Event                         Aggregated content
    ============================  =================================================
    ``message_update``             text_delta -> text delta; reasoning_delta -> reasoning
    ``tool_execution_start``       record one tool call (id / name / args)
    ``tool_execution_update``      record the partial_result during tool execution
    ``tool_execution_end``         record the tool result and is_error
    ``message_end``                fallback record of the final assistant message (no delta in run mode)
    ``turn_end``                   end the current turn and set that turn's text as the final answer
    ``agent_end``                  fallback: take the last assistant text from messages
    ============================  =================================================
    """

    def __init__(self, *, keep_events: bool = True) -> None:
        self.keep_events = keep_events
        self._unsubscribe: list[Callable[[], None]] = []
        self.result: AgentResult | None = None
        self.reset()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear the aggregation state of one request (called before each ``send()``)."""
        self._turn_text: list[str] = []
        self._turn_reasoning: list[str] = []
        self._streamed_text: list[str] = []
        self._final_text: str = ""
        self._final_reasoning: str = ""
        self._tool_calls: dict[str, dict[str, Any]] = {}
        self._tool_results: dict[str, dict[str, Any]] = {}
        self._events: list[AgentEvent] = []
        self.result = None

    def attach(self, bus: EventBus) -> list[Callable[[], None]]:
        """Register as a bus listener; return the list of callbacks usable to unsubscribe."""
        self.detach()
        self._unsubscribe.append(bus.add_listener(self))
        return list(self._unsubscribe)

    def detach(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()

    # ------------------------------------------------------------------
    # Reading results
    # ------------------------------------------------------------------

    @property
    def text(self) -> str:
        if self._final_text:
            return self._final_text
        if self._streamed_text:
            return "".join(self._streamed_text)
        if self.result is not None:
            return self.result.text
        return ""

    @property
    def reasoning(self) -> str:
        if self._final_reasoning:
            return self._final_reasoning
        if self.result is not None and self.result.response is not None:
            return self.result.response.reasoning_content
        return ""

    @property
    def streamed_text(self) -> str:
        return "".join(self._streamed_text)

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return list(self._tool_calls.values())

    @property
    def tool_results(self) -> list[dict[str, Any]]:
        return list(self._tool_results.values())

    def snapshot(
        self,
        *,
        session_id: str = DEFAULT_SESSION_ID,
        usage: dict[str, int] | None = None,
        result: AgentResult | None = None,
    ) -> AgentResponse:
        """Produce an immutable response snapshot for the caller to read or serialize."""
        collected = result if result is not None else self.result
        resolved_usage = dict(usage or (collected.usage if collected is not None else {}))
        return AgentResponse(
            text=self.text,
            reasoning=self.reasoning,
            streamed_text=self.streamed_text,
            tool_calls=self.tool_calls,
            tool_results=self.tool_results,
            usage=resolved_usage,
            session_id=session_id,
            events=list(self._events) if self.keep_events else [],
            result=collected,
        )


    # ------------------------------------------------------------------
    # AgentEventListener interface
    # ------------------------------------------------------------------

    def on_event(self, event: AgentEvent) -> None:  # type: ignore[override]
        """Receive all events synchronously; see the class docstring for the dispatch rules."""
        event_type = event.get("type") if isinstance(event, dict) else None

        if self.keep_events:
            self._events.append(event)

        if event_type == EVENT_TURN_START:
            self._turn_text.clear()
            self._turn_reasoning.clear()
        elif event_type == EVENT_MESSAGE_UPDATE:
            self._on_message_update(event)
        elif event_type == EVENT_MESSAGE_END:
            self._capture_final(event.get("message"))
        elif event_type == EVENT_TURN_END:
            self._on_turn_end(event)
        elif event_type == EVENT_AGENT_END:
            self._on_agent_end(event)
        elif event_type == EVENT_TOOL_START:
            self._on_tool_start(event)
        elif event_type == EVENT_TOOL_UPDATE:
            self._on_tool_update(event)
        elif event_type == EVENT_TOOL_END:
            self._on_tool_end(event)

    # ------------------------------------------------------------------
    # Event handling details
    # ------------------------------------------------------------------

    def _on_message_update(self, event: AgentEvent) -> None:
        update = event.get("assistant_message_event") or {}
        kind = update.get("type")
        text = update.get("text") or ""

        if kind == "reasoning_delta":
            self._turn_reasoning.append(text)
        elif kind == "text_delta":
            self._turn_text.append(text)
            self._streamed_text.append(text)
        else:
            # Fallback: parse the delta from message when there is no assistant_message_event.
            message = _assistant_from(event.get("message"))
            if message is not None and message.reasoning_content:
                self._turn_reasoning.append(message.reasoning_content)
            if message is not None and message.content:
                self._turn_text.append(message.content)
                self._streamed_text.append(message.content)

    def _on_turn_end(self, event: AgentEvent) -> None:
        # When this turn has streaming deltas, use the text assembled from the deltas as the final answer.
        if self._turn_text:
            self._final_text = "".join(self._turn_text)
        if self._turn_reasoning:
            self._final_reasoning = "".join(self._turn_reasoning)
        # When there are no deltas (run mode), fall back to the assistant message carried by turn_end.
        self._capture_final(event.get("message"))
        self._record_message_tool_results(event.get("tool_results") or [])
        self._turn_text.clear()
        self._turn_reasoning.clear()

    def _capture_final(self, message: Any) -> None:
        assistant = _assistant_from(message)
        if assistant is None:
            return
        if assistant.content:
            self._final_text = assistant.content
        if assistant.reasoning_content:
            self._final_reasoning = assistant.reasoning_content

    def _on_agent_end(self, event: AgentEvent) -> None:
        # Fallback: messages is the full execution context; the last assistant text is the final answer.
        for message in reversed(event.get("messages") or []):
            assistant = _assistant_from(message)
            if assistant is not None and assistant.content:
                self._final_text = assistant.content
                if assistant.reasoning_content:
                    self._final_reasoning = assistant.reasoning_content
                break

    def _on_tool_start(self, event: AgentEvent) -> None:
        call_id = str(event.get("tool_call_id") or "")
        self._tool_calls[call_id] = {
            "id": call_id,
            "name": event.get("tool_name") or "",
            "arguments": event.get("args"),
            "partial_result": None,
        }
        self._tool_results.setdefault(call_id, {
            "tool_call_id": call_id,
            "tool_name": event.get("tool_name") or "",
            "content": None,
            "is_error": None,
        })

    def _on_tool_update(self, event: AgentEvent) -> None:
        call_id = str(event.get("tool_call_id") or "")
        entry = self._tool_calls.get(call_id)
        if entry is not None:
            entry["partial_result"] = event.get("partial_result")

    def _on_tool_end(self, event: AgentEvent) -> None:
        call_id = str(event.get("tool_call_id") or "")
        self._tool_results[call_id] = {
            "tool_call_id": call_id,
            "tool_name": event.get("tool_name") or "",
            "content": event.get("result"),
            "is_error": bool(event.get("is_error")),
        }

    def _record_message_tool_results(self, results: Iterable[Any]) -> None:
        for item in results:
            call_id = str(getattr(item, "tool_call_id", "") or "")
            self._tool_results[call_id] = {
                "tool_call_id": call_id,
                "tool_name": getattr(item, "tool_name", "") or "",
                "content": getattr(item, "content", None),
                "is_error": bool(getattr(item, "is_error", False)),
            }


# ----------------------------------------------------------------------
# Agent facade: initialization / registration / user message entry / event handling
# ----------------------------------------------------------------------


class AgentRunner:
    """The uniform entry point for hosts such as CLI / TUI / Web to access the Agent.

    Construction does four things at once:

    1. **Agent initialization**: ``config.yaml`` -> ``ConfigStore`` -> ``LLMConfig`` ->
       ``LLMClient``; you can also inject ``client`` / ``llm_config`` directly (convenient for tests
       or for a host to reuse a connection).
    2. **Context storage**: ``ContextStore`` holds a ``Session`` (messages + workspace + Tool/Skill
       config + token usage). **Construction actively searches for a session but does not load
       content or create files**:

       - Search priority: explicit ``session_id`` / ``context_path`` > search by
         ``session_priority`` (**workspace first by default**; only when the workspace has none does
         it fall back to the user home directory, comparing the ``workspace`` recorded in the file
         and treating a session from another workspace as a miss) > a new ``uuid4``; a hit is
         recorded in ``found_context_path``;
       - Continuing an existing session requires **explicitly** calling ``load_session()`` (or
         ``load_context(path)``); while ``has_unloaded_session`` is true, sending / persisting errors
         out first to avoid overwriting existing history;
       - Starting a new conversation requires **explicitly** calling ``create_context()``: when
         ``session_id`` is not specified it generates a new ``uuid4`` every time, so it never
         overwrites / resets other sessions;
       - Persisting happens via ``save_context()``, or ``close()`` (only when this round really
         changed something), writing two copies to the user home directory and the workspace at
         ``.agent/sessions/<session_id>.json``.
       - Listing all sessions uses ``list_sessions()``: read-only summaries (``SessionInfo``: id /
         file path / location / time / message count / round count / usage / metadata) without
         loading content or creating files.

    3. **Tool registration**: the built-in ``read_file`` / ``list_files`` / ``write_file``
       (optionally ``bash``) are registered into the ``ToolRegistry`` and extended via
       ``extra_tools``; tools with ``requires_approval=True`` (``bash``) are approved in the
       ``ToolRegistry`` through the session's ``approve`` callback -- this class always passes a
       callback (``create_approve_callback()``); when ``auto_approve=True`` the callback returns
       ``True`` directly (no notice, no prompt), otherwise it shows the risk and asks the user to
       confirm.
    4. **Skill registration**: ``SkillRegistry`` discovers SKILL.md -> writes to ``session.skills``
       -> registers the ``load_skill`` / ``load_reference`` tools -> injects the index into the
       system prompt.
    5. **Default system prompt**: when ``use_default_system_prompt=True`` the default template of
       ``linteum_agent.prompt`` renders the system prompt (pre-filled with the tool list / Skill
       list / user system environment) and injects it; the Skill index is then no longer injected
       separately (the template already contains the Skill list).

    Afterwards send user messages with ``send()`` / ``asend()`` and consume them in a streaming way
    with ``stream()`` / ``astream()``; the response is aggregated by ``ResponseCollector`` from the
    ``EventBus`` events, so just read ``response.text``. Input and output are entirely up to the
    host; this class makes no CLI assumptions.
    """

    def __init__(
        self,
        *,
        # --- Agent initialization ---
        client: Any | None = None,
        llm_config: LLMConfig | None = None,
        config_store: ConfigStore | None = None,
        config_path: str | Path | None = None,
        # --- session / context storage ---
        session_id: str | None = None,
        workspace: str | Path | None = None,
        store: ContextStore | None = None,
        persist: bool = True,
        reuse_existing: bool = True,
        session_priority: str = DEFAULT_SESSION_PRIORITY,
        project_dir: str | Path | None = None,
        session_dirname: str | Path = DEFAULT_SESSION_DIRNAME,
        context_path: str | Path | None = None,
        # --- system prompt / extra context ---
        system_prompt: MessageInputs | str | None = None,
        context: MessageInputs | None = None,
        # When enabled, use the default template (pre-filled tool list / Skill list / user environment) as the system prompt.
        use_default_system_prompt: bool = True,
        # --- Tool registration ---
        include_builtin_tools: bool = True,
        enable_bash: bool = False,
        bash_timeout: float = 30.0,
        extra_tools: Iterable[Tool] = (),
        tool_enabled: Iterable[str] | None = None,
        tool_disabled: Iterable[str] | None = None,
        # --- tool approval (requires_approval, e.g. bash) ---
        auto_approve: bool = False,
        # --- Skill registration ---
        enable_skills: bool = True,
        skill_tools: bool = True,
        system_skill_dirs: Iterable[str | Path] | None = None,
        project_skill_dirs: Iterable[str | Path] | None = None,
        # --- engine / events ---
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
        events_bus: EventBus | None = None,
        keep_events: bool = True,
        save_on_close: bool = True,
        **engine_kwargs: Any,
    ) -> None:
        self.workspace = (
            Path(workspace).expanduser().resolve() if workspace is not None else Path.cwd()
        )
        self.persist = persist
        self.reuse_existing = reuse_existing
        # Session lookup priority: workspace first by default (workspace -> project).
        self.session_priority = _normalize_session_priority(session_priority)
        self.project_dir = (
            Path(project_dir).expanduser() if project_dir is not None else default_session_root()
        )
        self.session_dirname = Path(session_dirname)
        # Explicit single-file path: once given, only this one is read/written instead of two copies.
        self.context_path = (
            Path(context_path).expanduser() if context_path is not None else None
        )
        self.save_on_close = save_on_close
        self.loaded_context_paths: list[Path] = []
        self.saved_context_paths: list[Path] = []
        # Whether there are unsaved changes (decides whether close() writes files).
        self._dirty = False
        # The context file actively found during construction (None = nothing found, treat as a new session).
        self.found_context_path: Path | None = None
        # Records the path when "bound to an existing session on disk but not yet explicitly loaded":
        # in that case sending / persisting would overwrite existing history, so _begin_turn() /
        # save_context() guard against it.
        self._unloaded_session_file: Path | None = None
        # This instance's session ID: use the explicitly passed one; otherwise start with a uuid4 and
        # let _lookup_session() decide (it is rewritten to the found workspace session's ID).
        self.session_id = str(session_id) if session_id is not None else new_session_id()

        # Tool approval callback: tools with requires_approval=True (such as bash) go through it before
        # execution. When auto_approve=True the callback returns True directly (no risk notice, no user
        # prompt); otherwise it shows the risk and asks the user to confirm. Either way the callback is
        # bound to the session (ToolRegistry reads session.approve), so it never stays empty and causes
        # a PermissionError.
        self.auto_approve = bool(auto_approve)
        self.approve: ApproveFn = create_approve_callback(auto_approve=self.auto_approve)

        # 1) Agent initialization: an injected client wins, then an explicit llm_config, finally the config file.
        self.config_store = config_store
        self.llm_config = llm_config
        if client is not None:
            self.client = client
            if self.llm_config is None:
                self.llm_config = getattr(client, "config", None)
        else:
            if self.config_store is None:
                self.config_store = ConfigStore(config_path)
            if self.llm_config is None:
                self.llm_config = self.config_store.load(create=False).llm
            self.client = LLMClient(self.llm_config)

        # 2) Session / context storage: construction **actively searches** for a session (deciding the
        #    session_id and file location) but does **not load content or create files**; loading
        #    requires an explicit call to load_session().
        self.store = store or ContextStore(workspace=self.workspace)
        if self.store.default_workspace is None:
            self.store.default_workspace = self.workspace
        self._lookup_session(session_id)
        self.session = self.store.ensure_session(self.session_id, workspace=self.workspace)
        # Bind the approval callback to the session: ToolRegistry reads it through session.approve.
        self.session.approve = self.approve
        if tool_enabled is not None or tool_disabled is not None:
            self.session.configure_tools(enabled=tool_enabled, disabled=tool_disabled)
        self.saved_context_paths = list(self.loaded_context_paths)

        # 3) Events: build the bus first and attach the aggregating listener; later registration actions can also be observed.
        self.events_bus = events_bus or EventBus()
        self.collector = ResponseCollector(keep_events=keep_events)
        self.collector.attach(self.events_bus)

        # 4) Skill registration -> get the index injectable into the system prompt.
        self.skill_index = self._register_skills(
            enable_skills=enable_skills,
            system_skill_dirs=system_skill_dirs,
            project_skill_dirs=project_skill_dirs,
        )

        # 5) Tool registration (including Skill tools); the approval callback is likewise passed to ToolRegistry.
        self.registry = self._register_tools(
            include_builtin_tools=include_builtin_tools,
            enable_bash=enable_bash,
            bash_timeout=bash_timeout,
            extra_tools=extra_tools,
            skill_tools=skill_tools,
            approve=self.approve,
        )

        # 6) Default system prompt (optional): pre-fill tools / Skills / user environment with the default template.
        self.use_default_system_prompt = use_default_system_prompt
        self.default_system_prompt = (
            self.build_default_system_prompt() if use_default_system_prompt else ""
        )

        # 7) Middleware + engine.
        self.middleware = ContextMiddleware(
            self.client,
            self.store,
            context=self._build_context(
                system_prompt, context, self.skill_index, self.default_system_prompt
            ),
            workspace=self.workspace,
        )
        self.engine = AgentEngine(
            self.client,
            middleware=self.middleware,
            tool_registry=self.registry,
            events_bus=self.events_bus,
            max_tool_rounds=max_tool_rounds,
            **engine_kwargs,
        )
        self._last_result: AgentResult | None = None

    # ------------------------------------------------------------------
    # Session lookup (called actively during construction) and loading (explicit)
    # ------------------------------------------------------------------

    def _lookup_session(self, session_id: str | None) -> None:
        """**Actively search** for a session: decide ``session_id`` and record ``found_context_path``.

        Only a read-only search is performed (at most one read of an existing JSON to confirm the
        session ID inside it); it **never loads content into the store, nor creates any file**.
        Resolution order:

        1. Explicit ``session_id``: use it directly and record its existing copies (if any);
        2. ``persist=False``: never touch the disk, use a new uuid4;
        3. Explicit ``context_path``: if it exists use the session ID in that file, otherwise create
           a new uuid4;
        4. The injected ``store`` already has sessions: use the first one (content is already in
           memory);
        5. ``reuse_existing=False``: do not search, use a new uuid4 directly;
        6. Otherwise search by ``session_priority`` (**workspace first by default**; when falling back
           to the user level, compare the workspace recorded in the file and treat a session from
           another workspace as a miss); on a hit use that session;
        7. On no hit: use a new uuid4.

        A hit in case 1/3/6 is recorded in ``_unloaded_session_file``: the runner is then bound to
        that session but the content is not loaded yet, so ``load_session()`` (continue) or
        ``create_context()`` (start fresh) must be called before sending messages, to avoid
        overwriting existing history.
        """
        if session_id is not None:
            self.session_id = str(session_id)
            self._bind_found(self._find_session_path(self.session_id))
            return

        if not self.persist:
            self.session_id = new_session_id()
            return

        if self.context_path is not None:
            if self.context_path.is_file():
                self.session_id = self._loaded_session_id(self.context_path)
                self._bind_found(self.context_path)
                return
            self.session_id = new_session_id()
            return

        existing = self.store.sessions()
        if existing:
            # The injected store already has sessions: content is already in memory, no need to "load".
            self.session_id = str(existing[0])
            return

        if not self.reuse_existing:
            self.session_id = new_session_id()
            return

        found = self.find_context()
        if found is None:
            self.session_id = new_session_id()
            return

        self.session_id = self._loaded_session_id(found)
        self._bind_found(found)

    def _bind_found(self, path: Path | None) -> None:
        """Record "the found context file" and mark its content as not yet loaded."""
        self.found_context_path = path
        self._unloaded_session_file = path

    def _find_session_path(self, session_id: str) -> Path | None:
        """Return the existing context file of this session (by ``session_priority``: workspace first by default).

        When falling back to the user home directory it compares the ``workspace`` recorded in the
        file (see ``context_matches_workspace()``): a session from another workspace is not returned.
        """
        if self.context_path is not None:
            return self.context_path if self.context_path.is_file() else None
        candidates = find_context_jsons(
            self.workspace,
            project_dir=self.project_dir,
            dirname=self.session_dirname,
            session_id=session_id,
            priority=self.session_priority,
        )
        return candidates[0] if candidates else None

    @property
    def has_unloaded_session(self) -> bool:
        """Whether it is "bound to an existing session file but its content is not explicitly loaded yet".

        When ``True``, sending a message or persisting would overwrite that session's existing
        history, so ``send()`` / ``stream()`` / ``save_context()`` raise ``RuntimeError`` first,
        requiring the caller to choose explicitly: ``load_session()`` (continue that session) or
        ``create_context()`` (start a new conversation).
        """
        return self._unloaded_session_file is not None

    def _clear_unloaded_session(self) -> None:
        self._unloaded_session_file = None

    def load_session(
        self,
        session_id: str | None = None,
        *,
        path: str | Path | None = None,
        reuse_existing: bool | None = None,
    ) -> list[Path]:
        """**Explicitly** load a session and switch to it (construction only searches, never calls this method).

        Resolution order:

        1. ``path``: load only that file; return ``[]`` when the file does not exist (does not create
           a file);
        2. ``session_id``: among the session's two copies, load the **highest-priority** one by
           ``session_priority`` (the workspace copy by default); when there is no copy, return ``[]``
           but still switch the runner to that session (keeping an empty history, never writing to
           disk);
        3. Neither given: when ``reuse_existing`` (defaulting to the construction argument) is
           ``True``, prefer loading the session found during construction (``found_context_path``);
           otherwise search and load one by ``session_priority`` (which may switch sessions); when
           ``False``, do no searching.

        - When ``persist=False``, do no disk loading and return ``[]`` directly;
        - After a successful load, clear ``has_unloaded_session``;
        - After loading, rebind the tool registry to the loaded ``Session`` (``store.load`` rebuilds
          the session);
        - Return the list of file paths actually loaded (``[]`` means no file was loaded).
        """
        if not self.persist:
            return []

        reuse = self.reuse_existing if reuse_existing is None else bool(reuse_existing)

        if path is not None:
            target = Path(path).expanduser()
            if not target.is_file():
                return []
            self.store.load(target, merge=True)
            self.session_id = self._loaded_session_id(target)
            self.found_context_path = target
            self.loaded_context_paths = [target]
            self._clear_unloaded_session()
            self._rebind_session()
            return [target]

        if session_id is not None:
            self.session_id = str(session_id)
            target = self._find_session_path(self.session_id)
            if target is None:
                # No copy: switch to that session but keep an empty history, never write to disk.
                self.loaded_context_paths = []
                self._clear_unloaded_session()
                self._rebind_session()
                return []
            # The highest-priority copy (the workspace copy by default), not simply the newest by mtime.
            self.store.load(target, merge=True)
            self.found_context_path = target
            self.loaded_context_paths = [target]
            self._clear_unloaded_session()
            self._rebind_session()
            return [target]

        if not reuse:
            return []

        # A session was already found during construction: prefer loading it ("continue the session found during construction"), without switching to another session.
        pinned = self.found_context_path
        if pinned is not None and pinned.is_file():
            self.store.load(pinned, merge=True)
            self.loaded_context_paths = [pinned]
            self._clear_unloaded_session()
            self._rebind_session()
            return [pinned]

        found = self.find_context()
        if found is None:
            return []

        self.store.load(found, merge=True)
        self.session_id = self._loaded_session_id(found)
        self.found_context_path = found
        self.loaded_context_paths = [found]
        self._clear_unloaded_session()
        self._rebind_session()
        return [found]

    @staticmethod
    def _session_ids_in(path: Path) -> list[str]:
        """Read the list of session IDs in a context file (read-only; returns an empty list on a corrupt file / old format)."""
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if isinstance(sessions, dict):
            return [str(key) for key in sessions]
        return []

    def _loaded_session_id(self, path: Path) -> str:
        """Decide the session ID corresponding to the just-loaded file.

        By convention the file name ``<session_id>.json`` is the session ID; when the file name is
        not a session ID (for example an explicitly passed ``context.json``), take the first session
        in the file, and only fall back to the file name last.
        """
        ids = self._session_ids_in(path)
        stem = Path(path).stem
        if stem in ids:
            return stem
        return ids[0] if ids else stem

    def _rebind_session(self) -> None:
        """Rebind the current session object and the tool registry.

        ``store.load()`` rebuilds the ``Session`` object, so after every context load / create you
        must call ``ensure_session()`` and ``bind_session()`` again, otherwise the tools still point
        to the old session. The rebuilt session must also rebind the approval callback
        (``session.approve``), otherwise ``requires_approval`` tools such as ``bash`` raise
        ``PermissionError`` due to the missing ``approve``.
        """
        self.session = self.store.ensure_session(self.session_id, workspace=self.workspace)
        self.session.approve = self.approve
        self.registry.bind_session(self.session)




    # ------------------------------------------------------------------
    # Registration implementation
    # ------------------------------------------------------------------

    def _register_skills(
        self,
        *,
        enable_skills: bool,
        system_skill_dirs: Iterable[str | Path] | None,
        project_skill_dirs: Iterable[str | Path] | None,
    ) -> str:
        """Discover and register Skills; return the Skill index injectable into the system prompt.

        - Default system directory: ``~/.agent/skills``;
        - Default project directory: ``<workspace>/.agent/skills``;
        - When a directory does not exist, ``discover()`` skips it, so no error occurs when there are
          no Skills.
        """
        self.skill_registry: SkillRegistry | None = None
        if not enable_skills:
            return ""

        system_dirs = [
            Path(path).expanduser()
            for path in (
                system_skill_dirs if system_skill_dirs is not None else [_SYSTEM_SKILL_DIR]
            )
        ]
        project_dirs = [
            Path(path).expanduser()
            for path in (
                project_skill_dirs
                if project_skill_dirs is not None
                else [self.workspace / _PROJECT_SKILL_DIRNAME]
            )
        ]

        registry = SkillRegistry(system_skill_dirs=system_dirs, project_skill_dirs=project_dirs)
        registry.discover()
        registry.bind_session(self.session)
        self.skill_registry = registry
        return registry.build_prompt_index()

    def _register_tools(
        self,
        *,
        include_builtin_tools: bool,
        enable_bash: bool,
        bash_timeout: float,
        extra_tools: Iterable[Tool],
        skill_tools: bool,
        approve: ApproveFn | None = None,
    ) -> ToolRegistry:
        """Register the built-in tools, Skill tools, and extra tools uniformly into the ``ToolRegistry``.

        ``approve`` is passed to the ``ToolRegistry`` as well (written into ``session.approve``);
        tools with ``requires_approval=True`` call it for approval confirmation before execution.
        """
        if include_builtin_tools:
            registry = create_builtin_tools(
                include_bash=enable_bash,
                bash_timeout=bash_timeout,
                session=self.session,
                approve=approve,
            )
        else:
            registry = ToolRegistry(session=self.session, approve=approve)

        if skill_tools and self.skill_registry is not None:
            for tool in (self.skill_registry.create_load_skill(),
                         self.skill_registry.create_load_reference()):
                if registry.get(tool.name) is None:
                    registry.register(tool)

        for tool in extra_tools or ():
            if registry.get(tool.name) is not None:
                raise ValueError(f"duplicate tool registration: {tool.name}")
            registry.register(tool)

        self.session.register_tools(registry.all())
        return registry

    def build_default_system_prompt(self) -> str:
        """Render the system prompt with the default template (tool list + Skill list + user system environment).

        Works with the construction argument ``use_default_system_prompt``: when enabled, one is
        rendered and injected during construction (see ``self.default_system_prompt``); when disabled
        this method can still be called to preview on demand.
        """
        return _build_default_system_prompt(tools=self.registry, skills=self.skill_registry)

    @staticmethod
    def _build_context(
        system_prompt: MessageInputs | str | None,
        context: MessageInputs | None,
        skill_index: str,
        default_system_prompt: str = "",
    ) -> list[MessageInput]:
        """Assemble the static context injected into ``ContextMiddleware``.

        Order: default system prompt -> system prompt -> Skill index -> caller's extra context. The
        middleware places it after "persisted history" and before "the current request" (see
        ``ContextMiddleware._messages``).

        When ``default_system_prompt`` is non-empty (``use_default_system_prompt=True``) it already
        contains the Skill list, so ``skill_index`` is not injected separately, avoiding duplication.
        """
        items: list[MessageInput] = []
        if default_system_prompt:
            items.append(SystemPrompt(content=default_system_prompt))
        if system_prompt is not None:
            if isinstance(system_prompt, str):
                items.append(SystemPrompt(content=system_prompt))
            else:
                items.extend(normalize_messages(system_prompt))
        if skill_index and not default_system_prompt:
            items.append(SystemPrompt(content=skill_index))
        if context is not None:
            items.extend(normalize_messages(context))
        return items

    @staticmethod
    def _as_prompt(message: MessageInputs | str) -> MessageInputs:
        """A string is treated directly as a user message; other types are handed to the engine as is."""
        if isinstance(message, str):
            return UserPrompt(content=message)
        return message


    # ------------------------------------------------------------------
    # User message entry
    # ------------------------------------------------------------------

    def send(
        self,
        message: MessageInputs | str,
        *,
        session_id: str | None = None,
        context: MessageInputs | None = None,
        metadata: dict[str, Any] | None = None,
        tool_registry: ToolRegistry | None = None,
        max_tool_rounds: int | None = None,
        **kwargs: Any,
    ) -> AgentResponse:
        """Send a user message synchronously; return the ``AgentResponse`` aggregated from events.

        ``message`` can be a string (automatically wrapped into a ``UserPrompt``) or any
        ``MessageInputs``; ``kwargs`` are passed through to ``AgentEngine.run()`` (such as ``tools``).
        """
        resolved_session = session_id or self.session_id
        self._begin_turn()
        self._last_result = None
        result = self.engine.run(
            self._as_prompt(message),
            context=context,
            session_id=resolved_session,
            metadata=metadata,
            tool_registry=tool_registry,
            max_tool_rounds=max_tool_rounds,
            **kwargs,
        )
        return self._finish(resolved_session, result)

    async def asend(
        self,
        message: MessageInputs | str,
        *,
        session_id: str | None = None,
        context: MessageInputs | None = None,
        metadata: dict[str, Any] | None = None,
        tool_registry: ToolRegistry | None = None,
        max_tool_rounds: int | None = None,
        **kwargs: Any,
    ) -> AgentResponse:
        """Asynchronous version of ``send()`` (can execute asynchronous tools)."""
        resolved_session = session_id or self.session_id
        self._begin_turn()
        self._last_result = None
        result = await self.engine.arun(
            self._as_prompt(message),
            context=context,
            session_id=resolved_session,
            metadata=metadata,
            tool_registry=tool_registry,
            max_tool_rounds=max_tool_rounds,
            **kwargs,
        )
        return self._finish(resolved_session, result)

    def stream(
        self,
        message: MessageInputs | str,
        *,
        session_id: str | None = None,
        context: MessageInputs | None = None,
        metadata: dict[str, Any] | None = None,
        tool_registry: ToolRegistry | None = None,
        max_tool_rounds: int | None = None,
        on_chunk: Callable[[AgentStreamChunk], None] | None = None,
        **kwargs: Any,
    ) -> Iterator[AgentStreamChunk]:
        """Send a user message in a streaming way; afterwards read the aggregated result with ``response()``.

        ``message_update`` events are dispatched through ``events_bus`` during iteration; the caller
        can either listen to events for real-time output or consume ``AgentStreamChunk``.
        """
        resolved_session = session_id or self.session_id
        self._begin_turn()
        self._last_result = None
        for chunk in self.engine.stream(
            self._as_prompt(message),
            context=context,
            session_id=resolved_session,
            metadata=metadata,
            tool_registry=tool_registry,
            max_tool_rounds=max_tool_rounds,
            **kwargs,
        ):
            if chunk.done and chunk.result is not None:
                self._last_result = chunk.result
            if on_chunk is not None:
                on_chunk(chunk)
            yield chunk

    async def astream(
        self,
        message: MessageInputs | str,
        *,
        session_id: str | None = None,
        context: MessageInputs | None = None,
        metadata: dict[str, Any] | None = None,
        tool_registry: ToolRegistry | None = None,
        max_tool_rounds: int | None = None,
        on_chunk: Callable[[AgentStreamChunk], None] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AgentStreamChunk]:
        """Asynchronous version of ``stream()``."""
        resolved_session = session_id or self.session_id
        self._begin_turn()
        self._last_result = None
        async for chunk in self.engine.astream(
            self._as_prompt(message),
            context=context,
            session_id=resolved_session,
            metadata=metadata,
            tool_registry=tool_registry,
            max_tool_rounds=max_tool_rounds,
            **kwargs,
        ):
            if chunk.done and chunk.result is not None:
                self._last_result = chunk.result
            if on_chunk is not None:
                on_chunk(chunk)
            yield chunk

    def response(
        self,
        session_id: str | None = None,
        *,
        result: AgentResult | None = None,
    ) -> AgentResponse:
        """Read the event aggregation result of the most recent request."""
        return self._finish(session_id or self.session_id, result or self._last_result)

    def _begin_turn(self) -> None:
        """Begin a user request: clear the event aggregation and mark it "changed" so ``close()`` persists.

        ``_dirty`` is the switch for "whether ``close()`` needs to write files": merely constructing an
        AgentRunner does not set it, so no empty context JSON is produced.

        If an existing session was **found** during construction but not yet explicitly loaded
        (``has_unloaded_session``), this errors out first: sending directly would make the model answer
        on an "empty history" and overwrite the session's existing content when persisting.
        """
        if self.has_unloaded_session:
            raise RuntimeError(
                f"Session {self.session_id} already has context on disk ({self._unloaded_session_file}) "
                "but it is not loaded yet: call load_session() to continue that session, or "
                "create_context() to start a new conversation, before sending messages (otherwise the "
                "existing history would be overwritten)."
            )
        self.collector.reset()
        self._last_result = None
        self._dirty = True

    def _finish(self, session_id: str, result: AgentResult | None) -> AgentResponse:
        self._last_result = result
        collected = result if result is not None else self.collector.result
        usage = (
            dict(collected.usage)
            if collected is not None and collected.usage
            else self.engine.get_session_usage(session_id)
        )
        return self.collector.snapshot(session_id=session_id, usage=usage, result=result)


    # ------------------------------------------------------------------
    # Context / events / lifecycle
    # ------------------------------------------------------------------

    def get_context(self, session_id: str | None = None) -> list[Any]:
        """Return the session message history (a provider-neutral list of ``Message``)."""
        return self.engine.get_context(session_id or self.session_id)

    def get_session(self, session_id: str | None = None) -> Session | None:
        """Return the ``Session`` (workspace + messages + Tool/Skill config + usage)."""
        return self.engine.get_session(session_id or self.session_id)

    def get_session_usage(self, session_id: str | None = None) -> dict[str, int]:
        """Return the session's cumulative token usage."""
        return self.engine.get_session_usage(session_id or self.session_id)

    def context_paths(self, session_id: str | None = None) -> list[Path]:
        """Return the list of context storage paths of this session.

        - When ``context_path`` was given at construction: return only that one file;
        - Otherwise: the user home directory + workspace copies of ``.agent/sessions/<session_id>.json``
          (de-duplicated automatically when the two root directories are the same).
        """
        if self.context_path is not None:
            return [self.context_path]
        return session_context_paths(
            session_id or self.session_id,
            self.workspace,
            project_dir=self.project_dir,
            dirname=self.session_dirname,
        )

    def save_context(self, path: str | Path | None = None) -> list[Path]:
        """Write the context to JSON; return the list of paths actually written.

        By default two copies are written (user home directory + workspace, the file name being the
        UUID); when ``path`` is passed only that one is written. A location that is not writable (for
        example a read-only repository directory) is skipped, and the success list is returned as long
        as one copy succeeds; only when all fail is an exception raised.

        If the existing session found during construction is **not yet explicitly loaded**
        (``has_unloaded_session``), this errors out first to avoid overwriting that session's existing
        content with an empty history (call ``load_session()`` or ``create_context()`` first).
        """
        if self.has_unloaded_session:
            raise RuntimeError(
                f"The existing context of session {self.session_id} ({self._unloaded_session_file}) is not loaded yet: "
                "call load_session() to continue that session, or create_context() to start a new "
                "conversation, before persisting."
            )
        targets = (
            [Path(path).expanduser()] if path is not None else self.context_paths()
        )
        written: list[Path] = []
        errors: list[OSError] = []
        for target in targets:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                self.store.save_session(target, self.session_id)
                written.append(target)
            except OSError as exc:
                errors.append(exc)
        if not written and errors:
            raise errors[0]
        return written

    def load_context(self, path: str | Path | None = None) -> list[Path]:
        """Load the context from JSON; return the list of paths actually loaded.

        - With ``path``: try only that file;
        - With ``context_path`` specified: try only that copy;
        - Otherwise: among the current session's two copies, choose the **highest-priority** one (the
          workspace copy by default);
        - If missing, return an empty list (does not create a file);
        - After a successful load, clear ``has_unloaded_session`` (the content is now in memory and can
          be persisted safely).
        """
        if path is not None:
            target = Path(path).expanduser()
            if not target.is_file():
                return []
            self.store.load(target, merge=True)
            self._clear_unloaded_session()
            return [target]

        if self.context_path is not None:
            candidates = [self.context_path] if self.context_path.is_file() else []
        else:
            candidates = find_context_jsons(
                self.workspace,
                project_dir=self.project_dir,
                dirname=self.session_dirname,
                session_id=self.session_id,
                priority=self.session_priority,
            )
        if not candidates:
            return []
        target = candidates[0]
        self.store.load(target, merge=True)
        self.found_context_path = target
        self._clear_unloaded_session()
        return [target]

    def find_context(
        self,
        session_id: str | None = None,
        *,
        priority: str | None = None,
    ) -> Path | None:
        """Find an existing context JSON (read-only; does not create any file).

        - With ``session_id``: find only that session's two copies and return the highest-priority one
          by ``priority``;
        - With ``context_path``: check only that file;
        - Otherwise: **scan the workspace first**, falling back to the user home directory only when
          the workspace has none, returning the highest-priority (default workspace) and then newest
          copy;
        - When falling back to the user home directory it compares the ``workspace`` recorded in the
          file: one that does not match ``self.workspace`` is **not** the context being searched for
          (the user level is shared by multiple workspaces), and ``None`` is returned directly;
        - Return ``None`` when nothing is found.

        When ``priority`` is omitted, the construction argument ``session_priority`` (default
        ``"workspace"``) is used.
        """
        resolved_priority = self.session_priority if priority is None else priority
        if session_id is not None:
            return find_context_json(
                self.workspace,
                project_dir=self.project_dir,
                dirname=self.session_dirname,
                session_id=session_id,
                priority=resolved_priority,
            )
        if self.context_path is not None:
            return self.context_path if self.context_path.is_file() else None
        return find_context_json(
            self.workspace,
            project_dir=self.project_dir,
            dirname=self.session_dirname,
            priority=resolved_priority,
        )

    def list_sessions(
        self,
        *,
        include_project: bool = True,
        only_current_workspace: bool = False,
        dedupe: bool = True,
        priority: str | None = None,
    ) -> list[SessionInfo]:
        """List a read-only summary of **all sessions** (a convenience entry point for module-level ``list_sessions()``).

        List every session under the workspace (by default including the user home directory): id /
        file path / location / time / message count / round count / usage / metadata, etc.
        (``SessionInfo``). **Read-only**: does not load content, does not create files.

        - ``persist=False``: pure in-memory mode does not read the disk and returns ``[]`` directly;
        - When ``context_path`` was given at construction: list only the sessions in that file
          (``scope="context"``);
        - For the semantics of ``include_project`` / ``only_current_workspace`` / ``dedupe`` see the
          module-level ``list_sessions()``; when ``priority`` is omitted the construction argument
          ``session_priority`` is used.
        """
        if not self.persist:
            return []
        if self.context_path is not None:
            target = Path(self.context_path).expanduser()
            if not target.is_file():
                return []
            return _sort_infos(
                [
                    _session_info(
                        session, target, SESSION_SCOPE_CONTEXT, workspace=self.workspace
                    )
                    for session in _sessions_in_file(target)
                ]
            )
        return list_sessions(
            self.workspace,
            project_dir=self.project_dir,
            dirname=self.session_dirname,
            include_project=include_project,
            only_current_workspace=only_current_workspace,
            dedupe=dedupe,
            priority=self.session_priority if priority is None else priority,
        )

    def create_context(
        self,
        session_id: str | None = None,
        *,
        metadata: dict[str, Any] | None = None,
        overwrite: bool = False,
    ) -> list[Path]:
        """**Actively** create a new context JSON and switch to it; return the list of written paths.

        This is the sole entry point for "creating context / starting a new conversation"; constructing
        an ``AgentRunner`` never creates a file.

        - ``session_id is None`` (default): generate a **new** ``uuid4`` -- every call is a brand-new
          conversation, never reusing / overwriting another session's ``session_id``;
        - Explicitly passing ``session_id``: only create (or write back) that session's copy; with
          ``overwrite=False`` an existing file keeps its original content;
        - By default two copies (user home directory + workspace); when ``context_path`` was given at
          construction only that one is created.
        """
        target_session = str(session_id) if session_id is not None else new_session_id()
        paths = create_context_json(
            target_session,
            self.workspace,
            path=self.context_path,
            project_dir=self.project_dir,
            dirname=self.session_dirname,
            metadata=metadata,
            overwrite=overwrite,
        )
        self.session_id = target_session
        self.found_context_path = self._find_session_path(self.session_id)
        self.loaded_context_paths = self.load_context()
        self._clear_unloaded_session()
        self._rebind_session()
        self.saved_context_paths = list(paths)
        self._dirty = False
        return paths

    def clear_context(self, session_id: str | None = None) -> None:
        """Clear the session history (Tool/Skill config and usage are rebuilt along with the session)."""
        self.middleware.clear_context(session_id or self.session_id)

    def on(self, event_type: str, handler: Callable[[AgentEvent], Any]) -> Callable[[], None]:
        """Subscribe to a specific event type; return an unsubscribe callback (forwarded to ``EventBus.on``)."""
        return self.events_bus.on(event_type, handler)

    def add_listener(self, listener: AgentEventListener) -> Callable[[], None]:
        """Subscribe to all events (forwarded to ``EventBus.add_listener``)."""
        return self.events_bus.add_listener(listener)

    def tool_definitions(self) -> list[Any]:
        """Return the tool definitions provided to the LLM (affected by the session's enable/disable config)."""
        return self.registry.definitions()

    def skill_names(self) -> list[str]:
        """Return the names of the Skills in effect for this session."""
        return self.skill_registry.names() if self.skill_registry is not None else []

    def close(self) -> None:
        """Cancel event listening; persist the context per config only when "this run changed something"; safe to call repeatedly.

        Persist conditions: ``save_on_close=True`` + ``persist=True`` + ``self.dirty=True`` (a message
        was sent, or ``create_context()`` was called). So merely constructing an AgentRunner and then
        calling ``close()`` produces no context JSON.
        """
        if self.save_on_close and self.persist and self._dirty:
            self.saved_context_paths = self.save_context()
            self._dirty = False
        self.collector.detach()

    @property
    def dirty(self) -> bool:
        """Whether this run has unsaved changes (decides whether ``close()`` writes files)."""
        return self._dirty

    def __enter__(self) -> "AgentRunner":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


# ----------------------------------------------------------------------
# Convenience entry points
# ----------------------------------------------------------------------


def build_agent(**kwargs: Any) -> AgentRunner:
    """Factory function for ``AgentRunner`` (for hosts such as CLI / TUI / Web to reuse).

    Equivalent to ``AgentRunner(**kwargs)``; provided separately both to make the entry point clearer
    and to make it easy to swap the implementation later without changing caller code.
    """
    return AgentRunner(**kwargs)


def run_agent(
    message: MessageInputs | str,
    *,
    stream: bool = False,
    on_delta: Callable[[str], None] | None = None,
    close: bool = True,
    session_id: str | None = None,
    load: bool = False,
    **kwargs: Any,
) -> AgentResponse:
    """One-shot entry point: build the Agent -> send a message -> return the event-aggregated response.

    With ``stream=True`` the streaming engine is used and ``on_delta`` is called back with the text
    delta piece by piece; with ``close=True`` ``AgentRunner.close()`` is called (persisting the
    context per config).

    With ``load=True`` the session is **explicitly** loaded before sending: when ``session_id`` is
    given it loads that session, otherwise it searches for an existing context by
    ``reuse_existing`` / ``session_priority`` (the default ``load=False`` means a brand-new session
    every time, with no searching and no loading).
    """
    kwargs.setdefault("reuse_existing", load)
    runner = AgentRunner(session_id=session_id, **kwargs)
    try:
        if load:
            runner.load_session(session_id)
        if stream:
            for chunk in runner.stream(message):
                if on_delta is not None and chunk.delta:
                    on_delta(chunk.delta)
        else:
            runner.send(message)
        return runner.response()
    finally:
        if close:
            runner.close()
