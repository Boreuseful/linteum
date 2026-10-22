"""Run logger: a sidecar that records a detailed, structured run log (§48).

The framework already expresses *everything that happens* as an immutable
:class:`~linteum_agent.bus.event.Event`.  A log is therefore best built as an
**observation sidecar**: subscribe to ``*`` (``bus.on_any``), turn each event into
a record, and — for the few things that are not events (handler timing, dead
letters, publish rejections, WAL appends, final counters) — accept a small
structured feed from the bus via :meth:`InMemoryEventBus.add_log_sink`.

Writing is delegated to the standard :mod:`logging` module: the logger creates a
**dedicated, non-propagating** logger with a size-rotating file handler (and an
optional stderr handler), so it never touches the host's root configuration.
Records are rendered either as human ``key=value`` text (default) or as one JSON
object per line (``LogConfig.format="json"``).

Design constraints:

* **Zero third-party dependency** and no interference with the host's global
  ``logging`` configuration (``propagate=False``; we never add root handlers).
* **Never break the bus.**  Any I/O/serialisation failure degrades to a single
  ``stderr`` warning; the bus is untouched.
* **Metadata by default.**  Full (field-redacted) payloads are opt-in via
  ``LogConfig.include_content``; ``*.delta`` events are summarised, not flooded.

Every record carries the §48 correlation dimensions: ``trace_id`` / ``session_id``
/ ``event_id`` / ``call_id`` / ``origin`` (plus ``seq`` and ``cause_id``).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import OrderedDict
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, Mapping, Optional

from ...bus.event import payload_size
from .config import LogConfig

#: level name -> stdlib logging level (identical to the stdlib numbers).
_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO,
           "warning": logging.WARNING, "error": logging.ERROR}

#: Keys (whole or as a ``_``-separated component) whose *value* is masked.
SENSITIVE_COMPONENTS = frozenset(
    {
        "key", "token", "secret", "password", "passwd", "pwd",
        "authorization", "auth", "apikey", "cookie", "credential", "bearer",
    }
)

#: Fields that exist on every record (kept in a stable order for readability).
_BASE_FIELDS = (
    "ts", "level", "kind", "logger", "msg",
    "trace_id", "session_id", "event_id", "cause_id", "seq", "origin",
    "type", "call_id", "duration_ms", "status", "payload_size",
    "payload", "error", "error_info",
)

#: ``key=value`` extras appended to a text line (in this order).
_TEXT_EXTRAS = (
    "trace_id", "session_id", "event_id", "cause_id", "seq", "origin",
    "type", "call_id", "handler", "status", "duration_ms", "payload_size",
)


def _level_of(name: str) -> int:
    return _LEVELS.get(str(name).lower(), logging.INFO)


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------

def _is_sensitive(key: Any) -> bool:
    """True if ``key`` names a secret (component-wise, so ``api_key`` matches)."""
    text = str(key).lower()
    if text in SENSITIVE_COMPONENTS:
        return True
    parts = "".join(c if c.isalnum() else " " for c in text).split()
    return any(p in SENSITIVE_COMPONENTS for p in parts)


def _sanitize(
    value: Any,
    *,
    max_field_bytes: int,
    max_items: int,
    max_depth: int,
    depth: int = 0,
) -> Any:
    if depth > max_depth:
        return "…"
    if isinstance(value, str):
        if len(value) > max_field_bytes:
            return value[:max_field_bytes] + "…"
        return value
    if isinstance(value, Mapping):
        out: dict = {}
        for index, (k, v) in enumerate(value.items()):
            if index >= max_items:
                out["…"] = f"+{len(value) - index} more"
                break
            if _is_sensitive(k):
                out[str(k)] = "***"
            else:
                out[str(k)] = _sanitize(
                    v, max_field_bytes=max_field_bytes,
                    max_items=max_items, max_depth=max_depth, depth=depth + 1,
                )
        return out
    if isinstance(value, (list, tuple)):
        out_list = [
            _sanitize(v, max_field_bytes=max_field_bytes,
                      max_items=max_items, max_depth=max_depth, depth=depth + 1)
            for v in list(value)[:max_items]
        ]
        if len(value) > max_items:
            out_list.append(f"+{len(value) - max_items} more")
        return out_list
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return _sanitize(str(value), max_field_bytes=max_field_bytes,
                     max_items=max_items, max_depth=max_depth, depth=depth + 1)


def redact(
    payload: Any,
    *,
    max_field_bytes: int = 4096,
    max_items: int = 100,
    max_depth: int = 6,
) -> Any:
    """Return a JSON-safe copy of ``payload`` with secrets masked / fields clipped."""
    return _sanitize(
        payload,
        max_field_bytes=max_field_bytes,
        max_items=max_items,
        max_depth=max_depth,
    )


# ---------------------------------------------------------------------------
# formatters
# ---------------------------------------------------------------------------

class _TextFormatter(logging.Formatter):
    """Render a record's structured fields as one readable ``.log`` line."""

    def __init__(self) -> None:
        super().__init__(datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        fields = getattr(record, "run_fields", None)
        if fields is None:
            return super().format(record)
        stamp = self.formatTime(record, self.datefmt)
        line = f"{stamp} {record.levelname:<7} {str(fields.get('kind', '?')):<11} {fields.get('msg', '')}"
        extras = [f"{key}={fields[key]}" for key in _TEXT_EXTRAS if fields.get(key) is not None]
        if extras:
            line += " | " + " ".join(extras)
        for key in ("error", "error_info", "payload", "counters", "recent"):
            value = fields.get(key)
            if value is not None:
                line += f" | {key}={self._render(value)}"
        return line

    @staticmethod
    def _render(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            return str(value)


class _JsonFormatter(logging.Formatter):
    """Render each record as a single JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        fields = getattr(record, "run_fields", None)
        if fields is None:
            fields = {"ts": record.created, "level": record.levelname.lower(),
                      "msg": record.getMessage()}
        try:
            return json.dumps(fields, ensure_ascii=False, allow_nan=False, default=str)
        except Exception:  # noqa: BLE001
            return json.dumps({"ts": record.created, "level": record.levelname.lower(),
                               "msg": str(record.getMessage())})


# ---------------------------------------------------------------------------
# run logger
# ---------------------------------------------------------------------------

class RunLogger:
    """Sidecar writer of a structured run log, backed by stdlib :mod:`logging`.

    Usage::

        rig = RunLogger.attach(bus)            # reads AGENT_LOG_* env vars
        # or
        rig = RunLogger(bus, LogConfig(include_content=True))
        ...
        rig.close()
    """

    _seq = 0  # keeps dedicated logger names unique across instances

    def __init__(self, bus=None, config: Optional[LogConfig] = None) -> None:
        self.config = config or LogConfig.from_env()
        self.bus = None
        self._closed = False
        self._degraded = False
        self._unsub: Optional[Callable[[], None]] = None
        self._sink: Optional[Callable[[dict], None]] = None
        self._deltas: "OrderedDict[str, list]" = OrderedDict()
        self._logger = self._build_logger()
        if bus is not None:
            self._bind(bus)

    # -- construction helpers ----------------------------------------------
    @classmethod
    def attach(cls, bus, config: Optional[LogConfig] = None) -> "RunLogger":
        """Create a logger and attach it to ``bus`` in one call."""
        return cls(bus, config)

    def _build_logger(self) -> logging.Logger:
        """A dedicated, non-propagating logger with a rotating file handler."""
        RunLogger._seq += 1
        logger = logging.getLogger(f"agent.run.{RunLogger._seq}")
        logger.setLevel(_level_of(self.config.level))
        logger.propagate = False           # never touch the host's root config
        logger.handlers = []               # start clean for this instance
        path = self.config.resolved_path
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            handler = RotatingFileHandler(
                path,
                maxBytes=max(0, self.config.max_bytes),
                backupCount=max(0, self.config.backups),
                encoding="utf-8",
            )
            handler.setFormatter(self._make_formatter())
            logger.addHandler(handler)
        except OSError as exc:  # unwritable path -> degrade, never raise
            self._degrade(exc)
        if self.config.console:
            console = logging.StreamHandler(sys.stderr)
            console.setFormatter(self._make_formatter())
            logger.addHandler(console)
        return logger

    def _make_formatter(self) -> logging.Formatter:
        if str(self.config.format).lower() == "json":
            return _JsonFormatter()
        return _TextFormatter()

    def _degrade(self, exc: BaseException) -> None:
        if self._degraded:
            return
        self._degraded = True
        try:
            sys.stderr.write(f"[linteum_agent.runtime.observability] run log disabled: {exc!r}\n")
        except Exception:  # pragma: no cover - stderr itself is gone
            pass

    # -- record scaffolding -------------------------------------------------
    def _record(self, *, kind: str, level: str, msg: str, ts: Optional[float] = None,
                **extra: Any) -> dict:
        record = {field: None for field in _BASE_FIELDS}
        record.update(
            {
                "ts": ts if ts is not None else time.time(),
                "level": level,
                "kind": kind,
                "logger": "agent",
                "msg": msg,
            }
        )
        record.update(extra)
        return record

    def _write(self, record: dict) -> None:
        if self._closed:
            return
        levelno = _level_of(record.get("level", "info"))
        try:
            if not self._logger.isEnabledFor(levelno):
                return
            self._logger.log(levelno, record.get("msg") or "", extra={"run_fields": record})
        except Exception as exc:  # noqa: BLE001 - logging must never break the bus
            self._degrade(exc)

    # -- attachment ---------------------------------------------------------
    def _bind(self, bus) -> None:
        self.bus = bus
        self._unsub = bus.on_any(self.on_event)
        add = getattr(bus, "add_log_sink", None)
        if add is not None:
            self._sink = self._on_hook_record
            add(self._sink)

    def close(self) -> None:
        """Flush delta summaries + a counter snapshot, then close the handlers."""
        if self._closed:
            return
        self._flush_deltas()
        self._write_metrics_snapshot()
        if self._unsub is not None:
            try:
                self._unsub()
            except Exception:  # noqa: BLE001
                pass
            self._unsub = None
        remove = getattr(self.bus, "remove_log_sink", None) if self.bus is not None else None
        if remove is not None and self._sink is not None:
            try:
                remove(self._sink)
            except Exception:  # noqa: BLE001
                pass
        self._write(self._record(kind="lifecycle", level="info", msg="logger closed"))
        self._closed = True
        for handler in list(self._logger.handlers):
            try:
                handler.flush()
                handler.close()
            except Exception:  # noqa: BLE001
                pass
            self._logger.removeHandler(handler)

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, *exc_info) -> bool:
        self.close()
        return False

    # -- event sidecar ------------------------------------------------------
    def on_event(self, ev) -> None:
        """Map one bus event to a record (called from ``bus.on_any``)."""
        try:
            self._on_event(ev)
        except Exception as exc:  # noqa: BLE001 - never break the bus
            self._degrade(exc)

    def _on_event(self, ev) -> None:
        etype = getattr(ev, "type", "")
        if etype.endswith(".delta"):
            self._on_delta(ev)
            return
        payload = getattr(ev, "payload", None) or {}
        code = payload.get("code") if isinstance(payload, Mapping) else None
        record = self._record(
            kind="event",
            level=self._level_for(ev, code),
            msg=self._summary(ev),
            ts=getattr(ev, "ts", None),
            trace_id=getattr(ev, "trace_id", None),
            session_id=getattr(ev, "session_id", None),
            event_id=getattr(ev, "id", None),
            cause_id=getattr(ev, "cause_id", None),
            seq=getattr(ev, "seq", None),
            origin=getattr(ev, "origin", None),
            type=etype,
            call_id=self._call_id(payload),
            status="error" if code else None,
            payload_size=self._size(payload),
            error=str(payload.get("error")) if code else None,
            error_info=payload.get("error_info") if isinstance(payload, Mapping) else None,
        )
        if self.config.include_content:
            record["payload"] = redact(
                payload, max_field_bytes=self.config.max_field_bytes
            )
        self._write(record)

    def _on_delta(self, ev) -> None:
        if self.config.log_deltas and self._logger.isEnabledFor(logging.DEBUG):
            self._write(self._record(
                kind="event", level="debug", msg=self._summary(ev),
                ts=getattr(ev, "ts", None),
                trace_id=getattr(ev, "trace_id", None),
                session_id=getattr(ev, "session_id", None),
                event_id=getattr(ev, "id", None),
                cause_id=getattr(ev, "cause_id", None),
                seq=getattr(ev, "seq", None),
                origin=getattr(ev, "origin", None),
                type=getattr(ev, "type", ""),
                status="ok",
                payload=redact(getattr(ev, "payload", None) or {},
                               max_field_bytes=self.config.max_field_bytes)
                if self.config.include_content else None,
            ))
            return
        payload = getattr(ev, "payload", None) or {}
        text = payload.get("text") if isinstance(payload, Mapping) else None
        count, chars = self._deltas.get(ev.type, (0, 0))
        self._deltas[ev.type] = (count + 1, chars + (len(text) if isinstance(text, str) else 0))

    def _flush_deltas(self) -> None:
        if not self._deltas:
            return
        for etype, (count, chars) in self._deltas.items():
            self._write(self._record(
                kind="event", level="info", status="ok", type=etype,
                msg=f"{etype} coalesced: {count} event(s), {chars} char(s)",
                count=count, chars=chars,
            ))
        self._deltas.clear()

    # -- internal feed (bus hooks) -----------------------------------------
    def _on_hook_record(self, record: dict) -> None:
        try:
            base = self._record(
                kind=record.get("kind", "internal"),
                level=record.get("level", "info"),
                msg=record.get("msg", ""),
            )
            base.update(record)
            self._write(base)
        except Exception as exc:  # noqa: BLE001
            self._degrade(exc)

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _level_for(ev, code: Optional[str]) -> str:
        etype = getattr(ev, "type", "")
        if code or etype.endswith(".failed"):
            return "warning"
        if etype.startswith("system."):
            return "warning"
        payload = getattr(ev, "payload", None)
        if etype == "agent.completed" and isinstance(payload, Mapping) \
                and payload.get("finish") == "error":
            return "error"
        return "info"

    @staticmethod
    def _call_id(payload: Any) -> Optional[str]:
        if isinstance(payload, Mapping) and payload.get("call_id") is not None:
            return str(payload.get("call_id"))
        return None

    @staticmethod
    def _size(payload: Any) -> Optional[int]:
        if not isinstance(payload, Mapping):
            return None
        try:
            return payload_size(payload)
        except Exception:  # noqa: BLE001 - non-serialisable payload
            return None

    @staticmethod
    def _summary(ev) -> str:
        etype = getattr(ev, "type", "?")
        seq = getattr(ev, "seq", None)
        session = getattr(ev, "session_id", None)
        payload = getattr(ev, "payload", None)
        code = payload.get("code") if isinstance(payload, Mapping) else None
        bits = [str(etype)]
        if seq is not None:
            bits.append(f"seq={seq}")
        if session is not None:
            bits.append(f"session={session}")
        if code:
            bits.append(f"code={code}")
        return " ".join(bits)

    def _write_metrics_snapshot(self) -> None:
        if self.bus is None:
            return
        metrics = getattr(self.bus, "metrics", None)
        if metrics is None:
            return
        try:
            counters = metrics.snapshot()
            recent = [list(item) for item in list(getattr(metrics, "recent", []))[-10:]]
        except Exception:  # noqa: BLE001
            return
        self._write(self._record(
            kind="metrics", level="info", status="ok",
            msg=f"metrics snapshot ({len(counters)} counter(s))",
            counters=counters, recent=recent,
        ))


__all__ = ["RunLogger", "redact", "SENSITIVE_COMPONENTS"]
