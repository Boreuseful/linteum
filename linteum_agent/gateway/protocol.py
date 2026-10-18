"""Wire protocol v1: commands, frames, validation and version negotiation (§5).

Front-ends never see internal :class:`~linteum_agent.bus.event.Event` objects.
They exchange two JSON envelope kinds over any transport:

``cmd``
    client → gateway.  The name is a *whitelisted* command; the gateway is the
    only component allowed to turn it into a bus event.

``result`` / ``frame``
    gateway → client.  ``result`` acknowledges a command (durable/queued), while
    ``frame`` carries projected session facts or ephemeral streaming deltas.

The module is deliberately transport- and runtime-free: ``transports/*`` import
only this module, and the client SDK does not import ``runtime``.  It does not
touch the bus; the only import from it is the dependency-free ``error_info``
helper used by :mod:`linteum_agent.gateway.errors`.
"""

from __future__ import annotations

import fnmatch
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from .errors import (
    E_BAD_FRAME,
    E_PAYLOAD_TOO_LARGE,
    E_PROTOCOL,
    E_UNKNOWN_COMMAND,
    GatewayError,
    LOCATION_PROTOCOL,
)

#: Major protocol version.  ``hello`` negotiates it; there is no minor version.
PROTOCOL_VERSION = 1

#: Feature flags advertised by ``hello``.
FEATURES: tuple = (
    "view",
    "events",
    "replay",
    "approval",
    "artifacts",
    "history",
)

#: Commands a client may send (§5.2).  Anything else is ``E_UNKNOWN_COMMAND``.
KNOWN_COMMANDS: frozenset = frozenset(
    {
        "hello",
        "session.create",
        "session.list",
        "session.get",
        "session.delete",
        "session.subscribe",
        "session.unsubscribe",
        "user.message",
        "user.interrupt",
        "approval.decide",
        "agent.resume",
        "history.read",
        "artifact.read",
        "ping",
    }
)

#: Fields the host owns; a client may never supply them (§5.2 / §6.8).
RESERVED_ARGS: frozenset = frozenset({"origin", "seq", "cause_id"})

#: Control frames bypass the subscription filter (they are protocol plumbing,
#: not projected facts).
CONTROL_FRAMES: frozenset = frozenset(
    {"hello.ok", "snapshot", "gap", "flow", "error", "pong", "bye", "history.page", "artifact.chunk"}
)

_SESSION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TRACE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_PROFILE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _bad(message: str, **context: Any) -> GatewayError:
    return GatewayError(
        E_BAD_FRAME, message, context=context, location=LOCATION_PROTOCOL
    )


def validate_session_id(session_id: Any) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID.match(session_id):
        raise _bad("invalid session_id", session_id=session_id)
    return session_id


def validate_trace_id(trace_id: Any) -> Optional[str]:
    if trace_id is None:
        return None
    if not isinstance(trace_id, str) or not _TRACE_ID.match(trace_id):
        raise _bad("invalid trace_id", trace_id=trace_id)
    return trace_id


def _string(args: Mapping, key: str, *, required: bool = False,
            allow_empty: bool = False) -> Optional[str]:
    value = args.get(key)
    if value is None:
        if required:
            raise _bad(f"missing argument: {key}")
        return None
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise _bad(f"argument {key!r} must be a non-empty string")
    return value


def _integer(args: Mapping, key: str, *, minimum: Optional[int] = None,
             maximum: Optional[int] = None) -> Optional[int]:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(f"argument {key!r} must be an integer")
    if minimum is not None and value < minimum:
        raise _bad(f"argument {key!r} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise _bad(f"argument {key!r} must be <= {maximum}")
    return value


def _json_mapping(args: Mapping, key: str) -> Optional[dict]:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _bad(f"argument {key!r} must be an object")
    try:
        json.dumps(dict(value), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise _bad(f"argument {key!r} must be JSON-serialisable") from exc
    return dict(value)


def _string_list(args: Mapping, key: str) -> tuple:
    value = args.get(key)
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise _bad(f"argument {key!r} must be a list of strings")
    return tuple(value)


def command_session_id(cmd: "Command") -> Optional[str]:
    """``session_id`` from the envelope or ``args`` (both spellings are legal)."""
    value = cmd.session_id or cmd.args.get("session_id")
    return validate_session_id(value) if value is not None else None


def validate_command(cmd: "Command") -> None:
    """Whitelist + per-command structural validation (§5.2 / §6.8)."""
    if cmd.name not in KNOWN_COMMANDS:
        raise GatewayError(
            E_UNKNOWN_COMMAND, f"unknown command: {cmd.name!r}",
            location=LOCATION_PROTOCOL,
        )
    reserved = RESERVED_ARGS.intersection(cmd.args)
    if reserved:
        raise GatewayError(
            E_PROTOCOL,
            f"reserved field(s) are host-owned: {', '.join(sorted(reserved))}",
            location=LOCATION_PROTOCOL,
        )
    args = cmd.args

    if cmd.name == "hello":
        protocol = args.get("protocol", PROTOCOL_VERSION)
        if isinstance(protocol, list):
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in protocol):
                raise _bad("hello.protocol must be an integer or a list of integers")
        elif isinstance(protocol, bool) or not isinstance(protocol, int):
            raise _bad("hello.protocol must be an integer or a list of integers")
        client = args.get("client")
        if client is not None and not isinstance(client, Mapping):
            raise _bad("hello.client must be an object")
        _string_list(args, "features")
        return

    if cmd.name == "session.create":
        workspace = _string(args, "workspace", required=True)
        if workspace is None:  # pragma: no cover - required already raised
            raise _bad("missing argument: workspace")
        session_id = args.get("session_id")
        if session_id is not None:
            validate_session_id(session_id)
        profile = args.get("profile")
        if profile is not None and (not isinstance(profile, str) or not _PROFILE.match(profile)):
            raise _bad("invalid profile name")
        _string(args, "title")
        _json_mapping(args, "metadata")
        return

    if cmd.name == "session.list":
        _integer(args, "limit", minimum=1)
        _integer(args, "offset", minimum=0)
        _string(args, "workspace")
        return

    if cmd.name in ("session.get", "session.delete", "session.subscribe",
                    "session.unsubscribe"):
        if command_session_id(cmd) is None:
            raise _bad(f"{cmd.name} requires session_id")
        if cmd.name == "session.subscribe":
            _integer(args, "after_seq", minimum=0)
            mode = args.get("mode", "view")
            if mode not in ("view", "events"):
                raise _bad("subscribe mode must be 'view' or 'events'")
            _string_list(args, "frames")
        return

    if cmd.name == "user.message":
        if command_session_id(cmd) is None:
            raise _bad("user.message requires session_id")
        _string(args, "text", required=True)
        _string(args, "client_message_id", required=True)
        _json_mapping(args, "metadata")
        return

    if cmd.name == "user.interrupt":
        if command_session_id(cmd) is None:
            raise _bad("user.interrupt requires session_id")
        _string(args, "reason")
        return

    if cmd.name == "approval.decide":
        if command_session_id(cmd) is None:
            raise _bad("approval.decide requires session_id")
        _string(args, "call_id", required=True)
        decision = args.get("decision")
        if decision not in ("grant", "deny", "granted", "denied"):
            raise _bad("approval.decide.decision must be grant or deny")
        _string(args, "reason")
        return

    if cmd.name == "agent.resume":
        if command_session_id(cmd) is None:
            raise _bad("agent.resume requires session_id")
        return

    if cmd.name == "history.read":
        if command_session_id(cmd) is None:
            raise _bad("history.read requires session_id")
        _integer(args, "after_seq", minimum=0)
        _integer(args, "limit", minimum=1)
        mode = args.get("mode", "view")
        if mode not in ("view", "events"):
            raise _bad("history mode must be 'view' or 'events'")
        return

    if cmd.name == "artifact.read":
        if command_session_id(cmd) is None:
            raise _bad("artifact.read requires session_id")
        ref = _string(args, "ref", required=True)
        if ref is not None and not ref.startswith("art:"):
            raise _bad("artifact.read.ref must start with 'art:'")
        _integer(args, "offset", minimum=0)
        _integer(args, "limit", minimum=1)
        return

    if cmd.name == "ping":
        return


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------

def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True, slots=True)
class Command:
    """A whitelisted client → gateway command (§5.1)."""

    id: str
    name: str
    args: dict = field(default_factory=dict)
    session_id: Optional[str] = None
    trace_id: Optional[str] = None
    v: int = PROTOCOL_VERSION
    kind: str = "cmd"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Command":
        if not isinstance(data, Mapping):
            raise _bad("envelope must be an object")
        if data.get("kind") != "cmd":
            raise _bad("envelope kind must be 'cmd'")
        version = data.get("v", PROTOCOL_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            raise _bad("envelope v must be an integer")
        cmd_id = data.get("id")
        if not isinstance(cmd_id, str) or not cmd_id or len(cmd_id) > 128:
            raise _bad("envelope id must be a non-empty string (<= 128 chars)")
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise _bad("envelope name must be a non-empty string")
        args = data.get("args") or {}
        if not isinstance(args, Mapping):
            raise _bad("envelope args must be an object")
        session_id = data.get("session_id")
        if session_id is not None:
            validate_session_id(session_id)
        trace_id = validate_trace_id(data.get("trace_id"))
        return cls(
            id=cmd_id,
            name=name,
            args=dict(args),
            session_id=session_id,
            trace_id=trace_id,
            v=version,
        )

    def to_dict(self) -> dict:
        data = {
            "v": self.v,
            "kind": "cmd",
            "id": self.id,
            "name": self.name,
            "args": self.args,
        }
        if self.session_id is not None:
            data["session_id"] = self.session_id
        if self.trace_id is not None:
            data["trace_id"] = self.trace_id
        return data


@dataclass(frozen=True, slots=True)
class Frame:
    """A gateway → client fact/stream/control frame (§5.1 / §5.3)."""

    id: str
    frame: str
    payload: dict = field(default_factory=dict)
    session_id: Optional[str] = None
    seq: Optional[int] = None
    ephemeral: bool = False
    replay: bool = False
    ordinal: int = 0
    v: int = PROTOCOL_VERSION
    kind: str = "frame"

    @classmethod
    def create(
        cls,
        frame: str,
        payload: Optional[dict] = None,
        *,
        session_id: Optional[str] = None,
        seq: Optional[int] = None,
        ephemeral: bool = False,
        replay: bool = False,
        ordinal: int = 0,
    ) -> "Frame":
        return cls(
            id=_new_id("f"),
            frame=frame,
            payload=dict(payload or {}),
            session_id=session_id,
            seq=seq,
            ephemeral=ephemeral,
            replay=replay,
            ordinal=ordinal,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Frame":
        if not isinstance(data, Mapping) or data.get("kind") != "frame":
            raise _bad("envelope kind must be 'frame'")
        frame = data.get("frame")
        if not isinstance(frame, str) or not frame:
            raise _bad("frame name must be a non-empty string")
        payload = data.get("payload") or {}
        if not isinstance(payload, Mapping):
            raise _bad("frame payload must be an object")
        return cls(
            id=str(data.get("id") or _new_id("f")),
            frame=frame,
            payload=dict(payload),
            session_id=data.get("session_id"),
            seq=data.get("seq"),
            ephemeral=bool(data.get("ephemeral", False)),
            replay=bool(data.get("replay", False)),
            ordinal=int(data.get("ordinal", 0) or 0),
            v=int(data.get("v", PROTOCOL_VERSION) or PROTOCOL_VERSION),
        )

    def to_dict(self) -> dict:
        data = {
            "v": self.v,
            "kind": "frame",
            "id": self.id,
            "frame": self.frame,
            "payload": self.payload,
            "ephemeral": self.ephemeral,
            "replay": self.replay,
            "ordinal": self.ordinal,
        }
        if self.session_id is not None:
            data["session_id"] = self.session_id
        if self.seq is not None:
            data["seq"] = self.seq
        return data


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The acknowledgement of one command (§5.1)."""

    reply_to: str
    ok: bool = True
    result: dict = field(default_factory=dict)
    error: Optional[dict] = None
    id: str = field(default_factory=lambda: _new_id("r"))
    v: int = PROTOCOL_VERSION
    kind: str = "result"

    @classmethod
    def success(cls, reply_to: str, result: Optional[dict] = None) -> "CommandResult":
        return cls(reply_to=reply_to, ok=True, result=dict(result or {}))

    @classmethod
    def failure(cls, reply_to: str, error: GatewayError | Mapping[str, Any]) -> "CommandResult":
        payload = error.to_dict() if isinstance(error, GatewayError) else dict(error)
        return cls(reply_to=reply_to, ok=False, result={}, error=payload)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CommandResult":
        if not isinstance(data, Mapping) or data.get("kind") != "result":
            raise _bad("envelope kind must be 'result'")
        return cls(
            reply_to=str(data.get("reply_to", "")),
            ok=bool(data.get("ok", False)),
            result=dict(data.get("result") or {}),
            error=dict(data["error"]) if isinstance(data.get("error"), Mapping) else None,
            id=str(data.get("id") or _new_id("r")),
            v=int(data.get("v", PROTOCOL_VERSION) or PROTOCOL_VERSION),
        )

    def to_dict(self) -> dict:
        data = {
            "v": self.v,
            "kind": "result",
            "id": self.id,
            "reply_to": self.reply_to,
            "ok": self.ok,
            "result": self.result,
        }
        if self.error is not None:
            data["error"] = self.error
        return data


# ---------------------------------------------------------------------------
# Encode / decode / filters
# ---------------------------------------------------------------------------

def encode(envelope: Command | CommandResult | Frame | Mapping[str, Any]) -> str:
    data = envelope.to_dict() if hasattr(envelope, "to_dict") else dict(envelope)
    return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def decode(raw: str | bytes, *, max_bytes: Optional[int] = None) -> dict:
    """Parse one JSON envelope, enforcing the command-size limit (§6.8)."""
    if isinstance(raw, bytes):
        if max_bytes is not None and len(raw) > max_bytes:
            raise GatewayError(
                E_PAYLOAD_TOO_LARGE, f"envelope exceeds {max_bytes} bytes",
                location=LOCATION_PROTOCOL,
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _bad("envelope is not valid UTF-8") from exc
    else:
        text = raw
        if max_bytes is not None and len(text.encode("utf-8")) > max_bytes:
            raise GatewayError(
                E_PAYLOAD_TOO_LARGE, f"envelope exceeds {max_bytes} bytes",
                location=LOCATION_PROTOCOL,
            )
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise _bad(f"invalid JSON envelope: {exc}") from exc
    if not isinstance(data, dict):
        raise _bad("envelope must be a JSON object")
    return data


def frame_matches(name: str, patterns: Iterable[str]) -> bool:
    """True when ``name`` matches any subscription glob (empty = all)."""
    patterns = tuple(patterns)
    if not patterns:
        return True
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def hello_payload(
    *,
    server: str = "linteum",
    limits: Optional[Mapping[str, Any]] = None,
    features: Iterable[str] = FEATURES,
) -> dict:
    return {
        "protocol": PROTOCOL_VERSION,
        "server": server,
        "features": list(features),
        "limits": dict(limits or {}),
    }


def error_frame(error: GatewayError | Mapping[str, Any],
                *, session_id: Optional[str] = None,
                seq: Optional[int] = None) -> Frame:
    payload = error.to_dict() if isinstance(error, GatewayError) else dict(error)
    return Frame.create("error", {"error_info": payload}, session_id=session_id, seq=seq)


__all__ = [
    "CONTROL_FRAMES",
    "Command",
    "CommandResult",
    "FEATURES",
    "Frame",
    "KNOWN_COMMANDS",
    "PROTOCOL_VERSION",
    "RESERVED_ARGS",
    "command_session_id",
    "decode",
    "encode",
    "error_frame",
    "frame_matches",
    "hello_payload",
    "validate_command",
    "validate_session_id",
    "validate_trace_id",
]
