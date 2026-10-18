"""``linteum`` command line entry point (§8 / Phase 4 CLI).

The CLI is a **front-end**, not a second runtime:

* ``serve`` exposes the gateway over stdio/WebSocket;
* ``chat`` runs the embedded stack (``AgentHost + GatewayServer + inproc``) and
  drives it exclusively through :class:`~linteum_agent.gateway.client.AgentClient`;
* ``sessions`` prints the catalogue.

Even embedded mode goes through the gateway (§3.5), so the CLI exercises the
same protocol/projection/backpressure path as the TUI and a browser client.
Approval prompts use explicit ``/approve`` / ``/deny`` commands so a streaming
frame printer and a line reader never race for stdin.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Optional

from ..config.meta import field_specs, get_spec
from ..config.schema import ConfigError, default_config
from ..config.service import ConfigService
from ..config.store import ConfigStore
from ..runtime.llm.config import PROVIDER_DEFAULTS
from ..gateway.client import AgentClient, ClientError, ConnectionClosed
from ..gateway.leases import FileLeaseManager
from ..gateway.protocol import Frame
from ..gateway.server import GatewayServer
from ..gateway.transports.inproc import inproc_pair
from ..gateway.transports.stdio import StdioTransport
from ..runtime.host import AgentHost

DEFAULT_WORKSPACE = "."


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="linteum")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the gateway host")
    group = serve.add_mutually_exclusive_group()
    group.add_argument("--stdio", action="store_true",
                       help="JSONL over stdin/stdout (parent process owns the pipes)")
    group.add_argument("--ws", action="store_true",
                       help="WebSocket server (default: loopback)")
    serve.add_argument("--host", default=None, help="WebSocket bind host")
    serve.add_argument("--port", type=int, default=None, help="WebSocket bind port")
    serve.add_argument("--config", default=None, help="explicit config.yaml path")
    serve.add_argument("--no-lease", action="store_true",
                       help="disable the cross-process single-writer lease")
    serve.add_argument("--allow-remote", action="store_true",
                       help="allow binding a non-loopback WebSocket address")

    chat = sub.add_parser("chat", help="embedded one-shot / REPL chat")
    chat.add_argument("-m", "--message", default=None,
                      help="send one message and exit (one-shot mode)")
    chat.add_argument("-w", "--workspace", default=DEFAULT_WORKSPACE,
                      help="session workspace (default: current directory)")
    chat.add_argument("-s", "--session-id", default=None,
                      help="resume an existing session id")
    chat.add_argument("-p", "--profile", default=None,
                      help="agent profile from agent.profiles")
    chat.add_argument("--config", default=None, help="explicit config.yaml path")
    chat.add_argument("--no-lease", action="store_true",
                      help="disable the cross-process single-writer lease")
    chat.add_argument("--show-reasoning", action="store_true",
                      help="print reasoning deltas")
    chat.add_argument("--timeout", type=float, default=None,
                      help="one-shot timeout in seconds (default: none)")

    sessions = sub.add_parser("sessions", help="list sessions in the catalogue")
    sessions.add_argument("--config", default=None, help="explicit config.yaml path")
    sessions.add_argument("--workspace", default=None, help="filter by workspace")
    sessions.add_argument("--limit", type=int, default=50)

    # -- config ---------------------------------------------------------
    config = sub.add_parser("config", help="inspect and edit the semi-permanent config")
    config_sub = config.add_subparsers(dest="config_command", required=True)

    init = config_sub.add_parser("init", help="write a default config file")
    init.add_argument("--force", action="store_true",
                      help="overwrite an existing file (a backup is kept)")
    init.add_argument("--json", action="store_true")
    _add_config_path(init)

    path = config_sub.add_parser("path", help="print the config file path")
    path.add_argument("--json", action="store_true")
    _add_config_path(path)

    listing = config_sub.add_parser("list", help="list effective values")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--show-origin", action="store_true",
                         help="show default/file/env/cli for each value")
    listing.add_argument("--section", default=None,
                         help="filter by section or key prefix, e.g. agent or gateway.limits")
    _add_config_path(listing)

    getter = config_sub.add_parser("get", help="read one value")
    getter.add_argument("key")
    getter.add_argument("--json", action="store_true")
    getter.add_argument("--reveal", action="store_true",
                        help="reveal a secret (local terminal only)")
    _add_config_path(getter)

    setter = config_sub.add_parser("set", help="validate and write one value")
    setter.add_argument("key")
    setter.add_argument("value",
                        help="value; map/list fields accept JSON, e.g. '{\"a\":1}'")
    setter.add_argument("--no-persist", action="store_true",
                        help="validate/diff without writing the file")
    setter.add_argument("--expected-revision", default=None,
                        help="reject the write if the file revision changed")
    setter.add_argument("--json", action="store_true")
    _add_config_path(setter)

    unsetter = config_sub.add_parser(
        "unset", help="reset one value to its schema default"
    )
    unsetter.add_argument("key")
    unsetter.add_argument("--no-persist", action="store_true")
    unsetter.add_argument("--expected-revision", default=None)
    unsetter.add_argument("--json", action="store_true")
    _add_config_path(unsetter)

    editor = config_sub.add_parser(
        "edit", help="open $VISUAL/$EDITOR and validate the file on save"
    )
    _add_config_path(editor)

    validator = config_sub.add_parser("validate", help="validate file / effective config")
    validator.add_argument("--effective", action="store_true",
                           help="validate the merged runtime view")
    validator.add_argument("--strict", action="store_true",
                           help="treat warnings as errors")
    validator.add_argument("--json", action="store_true")
    _add_config_path(validator)

    reloader = config_sub.add_parser("reload", help="re-read the file and show the diff")
    reloader.add_argument("--json", action="store_true")
    _add_config_path(reloader)

    applier = config_sub.add_parser(
        "apply", help="classify changes into live/new_session/restart"
    )
    applier.add_argument("--json", action="store_true")
    _add_config_path(applier)

    # -- setup ----------------------------------------------------------
    setup = sub.add_parser("setup", help="interactive provider/model/api-key setup")
    setup.add_argument("--provider", default=None)
    setup.add_argument("--model", default=None)
    setup.add_argument("--api-key", default=None,
                       help="literal key or ${ENV_VAR}; prefer the env reference")
    setup.add_argument("--base-url", default=None)
    setup.add_argument("--force", action="store_true",
                       help="update an existing config without prompting")
    setup.add_argument("--json", action="store_true")
    _add_config_path(setup)

    return parser


def _add_config_path(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="explicit config.yaml path")


def _build_service(path: Optional[str]) -> ConfigService:
    return ConfigService(ConfigStore(path) if path else None)


def _warn(message: str) -> None:
    print(f"linteum: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

async def _serve(args) -> int:
    service = _build_service(args.config)
    effective = service.effective(strict=False)
    cfg = effective.config
    for issue in effective.warnings:
        _warn(f"{issue.code}: {issue.message}")

    transport = "stdio" if args.stdio else "websocket" if args.ws \
        else cfg.gateway.transport
    if transport in ("auto", "inproc"):
        transport = "websocket"
    if transport not in ("stdio", "websocket"):
        _warn(f"unsupported transport for serve: {transport!r}")
        return 2

    host_name = args.host or cfg.gateway.websocket.host
    port = int(args.port or cfg.gateway.websocket.port)
    endpoint = (
        f"ws://{host_name}:{port}/" if transport == "websocket" else "stdio://"
    )
    lease_manager = None if args.no_lease else FileLeaseManager()

    host = AgentHost(
        effective,
        config_service=service,
        lease_manager=lease_manager,
        endpoint=endpoint,
    )
    await host.start()
    server = GatewayServer(host, config_service=service)
    try:
        if transport == "stdio":
            transport_impl = await StdioTransport.open()
            await server.serve(transport_impl)
        else:
            from ..gateway.websocket_server import serve_websocket
            import signal

            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop.set)
                except (NotImplementedError, RuntimeError):  # pragma: no cover
                    pass
            try:
                await serve_websocket(
                    server,
                    host=host_name,
                    port=port,
                    stop=stop,
                    allow_remote=args.allow_remote,
                )
            finally:
                stop.set()
    except asyncio.CancelledError:
        raise
    finally:
        await server.close()
        await host.close()
    return 0


# ---------------------------------------------------------------------------
# frame rendering
# ---------------------------------------------------------------------------

def _text_of(message) -> str:
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _brief(value, limit: int = 400) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[:limit] + "…"


class _Renderer:
    """Render semantic frames to a terminal (shared by one-shot and REPL)."""

    def __init__(self, *, show_reasoning: bool = False, stream=None) -> None:
        self.show_reasoning = show_reasoning
        self.out = stream or sys.stdout
        self._streamed = False
        self.failed = False

    def print(self, text: str = "", *, end: str = "\n") -> None:
        self.out.write(text + end)
        self.out.flush()

    def render(self, frame: Frame) -> None:
        name = frame.frame
        payload = frame.payload or {}
        if name == "assistant.started":
            self._streamed = False
        elif name == "assistant.delta":
            self._streamed = True
            self.print(payload.get("text", ""), end="")
        elif name == "reasoning.delta":
            if self.show_reasoning:
                self.print(f"\n[reasoning] {payload.get('text', '')}")
        elif name == "assistant.message":
            text = _text_of(payload.get("message"))
            if not self._streamed and text:
                self.print(text, end="")
            self.print()
            for call in payload.get("tool_calls") or []:
                self.print(f"[tool call] {call.get('name')} {_brief(call.get('args'))}")
        elif name == "tool.requested":
            self.print(f"[tool] {payload.get('name')} {_brief(payload.get('args'))}")
        elif name == "tool.result":
            status = "ok" if payload.get("ok") else f"failed {payload.get('code') or ''}".strip()
            self.print(f"[tool result] {payload.get('name')}: {status} {_brief(payload.get('output') or payload.get('error') or '')}")
        elif name == "approval.required":
            self.print(
                f"\n[approval required] call_id={payload.get('call_id')} "
                f"tool={payload.get('name')} args={payload.get('args_preview')}\n"
                f"  /approve {payload.get('call_id')}   /deny {payload.get('call_id')}"
            )
        elif name == "approval.resolved":
            self.print(f"[approval] {payload.get('call_id')} -> {payload.get('decision')}")
        elif name == "agent.completed":
            finish = payload.get("finish")
            self.print(f"\n[done] round={payload.get('round')} finish={finish}")
            if finish == "error":
                self.failed = True
                if payload.get("error_info"):
                    info = payload["error_info"]
                    self.print(f"[error] {info.get('code')}: {info.get('message')}")
        elif name == "error":
            self.failed = True
            info = payload.get("error_info") or {}
            self.print(f"\n[error] {info.get('code')}: {info.get('message')}")
        elif name == "notice":
            kind = payload.get("kind")
            if kind == "frame_too_large":
                self.print(f"[notice] frame too large: {payload.get('frame')}")
            elif kind == "input_failed":
                info = payload.get("error_info") or {}
                self.print(f"[error] queued message failed: {info.get('code')}: {info.get('message')}")
            else:
                self.print(f"[notice] {kind}")
        elif name == "compaction.applied":
            self.print("[context compacted]")
        elif name == "history.page":
            self.print(f"[history] {len(payload.get('frames') or [])} frame(s), "
                       f"next_seq={payload.get('next_seq')}")
            for inner in payload.get("frames") or []:
                try:
                    self.render(Frame.from_dict(inner))
                except Exception:  # noqa: BLE001 - history rendering is best effort
                    continue
        elif name in ("snapshot", "phase.changed", "flow", "event", "bye", "gap"):
            if name == "gap":
                self.print(f"[gap] missing seq {payload.get('from_seq')}..{payload.get('to_seq')}")
            elif name == "bye":
                self.print(f"[bye] {payload.get('reason')}")
        else:
            return


async def _print_frames(
    client: AgentClient,
    renderer: _Renderer,
    done: asyncio.Event,
    *,
    signal_on_completion: bool = False,
    target_client_message_id: Optional[str] = None,
) -> None:
    # One-shot waits for the completion of *its* message.  A queued message is
    # published later, so the printer arms only after the matching user.message
    # frame — otherwise a still-running previous turn would end the command.
    armed = not signal_on_completion
    try:
        async for frame in client.iter_frames():
            if frame.replay and frame.frame not in ("snapshot", "history.page"):
                continue  # replayed history is rendered on demand via /history
            renderer.render(frame)
            if not signal_on_completion or frame.replay:
                continue
            if (
                frame.frame == "user.message"
                and frame.payload.get("client_message_id") == target_client_message_id
            ):
                armed = True
            elif armed and frame.frame in ("agent.completed", "error"):
                done.set()
    except asyncio.CancelledError:
        return
    except (ConnectionClosed, ClientError):
        return
    except Exception:  # noqa: BLE001 - a rendering stream must not crash the CLI
        return
    finally:
        done.set()


# ---------------------------------------------------------------------------
# embedded chat
# ---------------------------------------------------------------------------

async def _chat(args) -> int:
    service = _build_service(args.config)
    effective = service.effective(strict=False)
    for issue in effective.warnings:
        _warn(f"{issue.code}: {issue.message}")
    workspace = Path(args.workspace).expanduser().resolve()
    lease_manager = None if args.no_lease else FileLeaseManager()

    host = AgentHost(
        effective, config_service=service, lease_manager=lease_manager,
        endpoint="inproc://cli",
    )
    await host.start()
    server = GatewayServer(host, config_service=service)
    client_end, server_end = inproc_pair()
    serve_task = asyncio.create_task(server.serve(server_end))
    client = AgentClient(client_end, client_id="cli")
    renderer = _Renderer(show_reasoning=args.show_reasoning)
    printer: Optional[asyncio.Task] = None
    session_id = args.session_id or f"cli-{uuid.uuid4().hex[:8]}"
    try:
        await client.connect()
        created = await client.create_session(
            str(workspace), session_id=session_id, profile=args.profile,
        )
        session_id = created["session"]["session_id"]
        await client.subscribe(session_id, after_seq=0)

        done = asyncio.Event()
        one_shot = bool(args.message)
        message_id = f"m-{uuid.uuid4().hex[:16]}" if one_shot else None
        printer = asyncio.create_task(
            _print_frames(
                client, renderer, done,
                signal_on_completion=one_shot,
                target_client_message_id=message_id,
            )
        )
        if one_shot:
            await client.send_message(
                session_id, args.message, client_message_id=message_id,
            )
            timeout = args.timeout
            try:
                if timeout:
                    await asyncio.wait_for(done.wait(), timeout=timeout)
                else:
                    await done.wait()
            except asyncio.TimeoutError:
                _warn(f"no completion within {timeout}s")
                return 3
            return 1 if renderer.failed else 0
        await _repl(client, renderer, session_id)
        return 0
    except ClientError as exc:
        _warn(f"{exc.code}: {exc}")
        return 1
    finally:
        if printer is not None:
            printer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await printer
        await client.close()
        with contextlib.suppress(asyncio.CancelledError):
            await serve_task
        await server.close()
        await host.close()


_REPL_HELP = """\
commands:
  /approve <call_id> [reason]   approve a pending tool call
  /deny <call_id> [reason]      deny a pending tool call
  /interrupt [reason]           interrupt the current round
  /resume                       resume a paused session
  /history [limit]              read recent history
  /session                      print the current session id
  /quit                         exit
anything else is sent as a user message.
"""


async def _repl(client: AgentClient, renderer: _Renderer, session_id: str) -> None:
    renderer.print(f"session {session_id} — /help for commands")
    while True:
        try:
            line = await asyncio.to_thread(input, "> ")
        except (EOFError, KeyboardInterrupt):
            renderer.print()
            return
        line = line.strip()
        if not line:
            continue
        try:
            if line in ("/quit", "/exit"):
                return
            if line == "/help":
                renderer.print(_REPL_HELP)
            elif line == "/session":
                renderer.print(session_id)
            elif line.startswith("/approve"):
                parts = line.split(maxsplit=2)
                if len(parts) < 2:
                    renderer.print("usage: /approve <call_id> [reason]")
                    continue
                await client.decide_approval(
                    session_id, parts[1], decision="grant",
                    reason=parts[2] if len(parts) > 2 else None,
                )
            elif line.startswith("/deny"):
                parts = line.split(maxsplit=2)
                if len(parts) < 2:
                    renderer.print("usage: /deny <call_id> [reason]")
                    continue
                await client.decide_approval(
                    session_id, parts[1], decision="deny",
                    reason=parts[2] if len(parts) > 2 else None,
                )
            elif line.startswith("/interrupt"):
                reason = line[len("/interrupt"):].strip() or None
                await client.interrupt(session_id, reason=reason)
            elif line == "/resume":
                await client.resume_agent(session_id)
            elif line.startswith("/history"):
                parts = line.split(maxsplit=1)
                limit = int(parts[1]) if len(parts) > 1 else 30
                await client.history(session_id, after_seq=0, limit=limit)
            elif line.startswith("/"):
                renderer.print("unknown command; /help for the list")
            else:
                await client.send_message(session_id, line)
        except ClientError as exc:
            renderer.print(f"[error] {exc.code}: {exc}")


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

async def _sessions(args) -> int:
    service = _build_service(args.config)
    effective = service.effective(strict=False)
    host = AgentHost(effective, config_service=service)
    try:
        infos = await host.list_sessions(
            limit=args.limit, workspace=args.workspace,
        )
        if not infos:
            print("(no sessions)")
            return 0
        for info in infos:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(info.updated_at))
            print(
                f"{info.session_id}\t{stamp}\t{info.phase}\t"
                f"{info.message_count} msg\t{info.workspace or ''}"
            )
        return 0
    finally:
        await host.close()


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def _format_value(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return json.dumps(value, ensure_ascii=False, default=str)


def _issue_line(issue) -> str:
    key = f" {issue.key}" if issue.key else ""
    hint = f" ({issue.hint})" if issue.hint else ""
    return f"{issue.severity}:{key} [{issue.code}] {issue.message}{hint}"


def _json_dump(payload) -> None:
    print(json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True, default=str,
    ))


def _config_error(exc: ConfigError, *, json_mode: bool) -> int:
    if json_mode:
        _json_dump({"ok": False, "error": exc.to_dict()})
    else:
        key = f" {exc.key}" if exc.key else ""
        hint = f" ({exc.hint})" if exc.hint else ""
        _warn(f"{exc.code}{key}: {exc}{hint}")
    return 1


def _print_apply(result) -> None:
    if result.applied_live:
        print("live:", ", ".join(result.applied_live))
    if result.requires_new_session:
        print("new_session:", ", ".join(result.requires_new_session))
    if result.requires_restart:
        print("restart:", ", ".join(result.requires_restart))
    for warning in result.warnings:
        print(_issue_line(warning))


def _section_matches(key: str, spec_section: str, wanted: str) -> bool:
    return (
        key == wanted
        or key.startswith(wanted + ".")
        or spec_section == wanted
        or spec_section.startswith(wanted + ".")
    )


def _filter_values(values, section: str):
    """Return the nested slice for a dotted section/key prefix (JSON list)."""
    current = values
    for part in section.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return {}
    return {section: current}


def _filter_sources(sources: dict, section: str) -> dict:
    return {
        key: source for key, source in sources.items()
        if key == section or key.startswith(section + ".")
    }


def _coerce_cli_value(key: str, raw: str):
    """Map ``null``/``none`` to None only for nullable schema fields."""
    spec = get_spec(key)
    if spec.nullable and raw.strip().lower() in ("null", "none", "~"):
        return None
    return raw


def _config_init(service: ConfigService, args) -> int:
    store = service.store
    if store.exists() and not args.force:
        _warn(f"{store.path} already exists; use --force to overwrite")
        return 1
    store.save(default_config())
    effective = service.effective(strict=False)
    if args.json:
        _json_dump({
            "ok": True,
            "path": service.path(),
            "revision": effective.revision,
            "issues": [issue.to_dict() for issue in effective.issues],
        })
        return 0
    print(f"wrote {store.path}")
    print(f"revision: {effective.revision}")
    for issue in effective.issues:
        print(_issue_line(issue))
    return 0


def _config_path(service: ConfigService, args) -> int:
    payload = {
        "path": service.path(),
        "revision": service.revision(),
        "exists": service.exists(),
    }
    if args.json:
        _json_dump(payload)
    else:
        print(payload["path"])
    return 0


def _config_list(service: ConfigService, args) -> int:
    effective = service.effective(strict=False)
    if args.json:
        values = effective.to_dict(reveal=False)
        sources = effective.sources_dict()
        if args.section:
            values = _filter_values(values, args.section)
            sources = _filter_sources(sources, args.section)
        payload = {
            "ok": True,
            "path": service.path(),
            "revision": effective.revision,
            "values": values,
            "issues": [issue.to_dict() for issue in effective.issues],
        }
        if args.section:
            payload["section"] = args.section
        if args.show_origin:
            payload["sources"] = sources
        _json_dump(payload)
        return 0

    print(f"# {service.path()}")
    if effective.revision:
        print(f"# revision {effective.revision}")
    current_section = None
    for spec in field_specs(effective.config):
        if args.section and not _section_matches(spec.key, spec.section, args.section):
            continue
        group = spec.section or spec.key.split(".")[0]
        if group != current_section:
            print(f"\n[{group}]")
            current_section = group
        value = effective.get(spec.key)
        if not value.set:
            shown = "<unset>"
        elif value.secret:
            shown = value.masked or "***"
        else:
            shown = _format_value(value.value)
        origin = f"  [{value.source}]" if args.show_origin else ""
        print(f"{spec.key} = {shown}{origin}")
    if effective.issues:
        print()
        for issue in effective.issues:
            print(_issue_line(issue))
    return 0


def _config_get(service: ConfigService, args) -> int:
    if args.reveal and not sys.stdout.isatty():
        _warn("--reveal is only allowed on a local terminal (stdout is not a TTY)")
        return 1
    value = service.get(args.key, reveal=args.reveal)
    if args.json:
        _json_dump(value.to_dict())
        return 0
    if not value.set:
        print("<unset>")
    elif value.secret and not args.reveal:
        print(value.masked or "***")
    else:
        print(_format_value(value.value))
    return 0


def _config_set(service: ConfigService, args) -> int:
    value = _coerce_cli_value(args.key, args.value)
    change_set = service.set(
        args.key,
        value,
        expected_revision=args.expected_revision,
        persist=not args.no_persist,
    )
    result = service.apply(change_set)
    if args.json:
        _json_dump({
            "ok": True,
            "revision": change_set.revision,
            "persisted": not args.no_persist,
            "changes": [change.to_dict() for change in change_set.changes],
            "apply": result.to_dict(),
        })
        return 0
    if not change_set.changes:
        print("no changes")
    else:
        for change in change_set.changes:
            print(
                f"{change.key}: {_format_value(change.old)} -> "
                f"{_format_value(change.new)}  [{change.scope}]"
            )
    print(f"revision: {change_set.revision or '<missing>'}")
    if args.no_persist:
        print("not persisted (--no-persist)")
    _print_apply(result)
    return 0


def _config_unset(service: ConfigService, args) -> int:
    change_set = service.unset(
        args.key,
        expected_revision=args.expected_revision,
        persist=not args.no_persist,
    )
    result = service.apply(change_set)
    if args.json:
        _json_dump({
            "ok": True,
            "revision": change_set.revision,
            "persisted": not args.no_persist,
            "changes": [change.to_dict() for change in change_set.changes],
            "apply": result.to_dict(),
        })
        return 0
    if not change_set.changes:
        print("no changes (already at the schema default)")
    else:
        for change in change_set.changes:
            print(
                f"{change.key}: {_format_value(change.old)} -> "
                f"{_format_value(change.new)}  [{change.scope}]"
            )
    print(f"revision: {change_set.revision or '<missing>'}")
    if args.no_persist:
        print("not persisted (--no-persist)")
    _print_apply(result)
    return 0


def _config_edit(service: ConfigService, args) -> int:
    store = service.store
    if not store.exists():
        store.save(default_config())
        _warn(f"created default config at {store.path}")
    # Snapshot the pre-edit view for the diff; an already-invalid file is left
    # editable (the post-edit validation is authoritative).
    try:
        service.effective(strict=False)
    except ConfigError:
        pass
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        _warn("set $VISUAL or $EDITOR to edit the config")
        return 1
    parts = shlex.split(editor)
    if not parts:
        _warn("$VISUAL/$EDITOR is empty")
        return 1
    try:
        completed = subprocess.run([*parts, str(store.path)])
    except OSError as exc:
        _warn(f"failed to launch editor: {exc}")
        return 1

    issues = service.validate()
    errors = [issue for issue in issues if issue.is_error]
    for issue in issues:
        print(_issue_line(issue), file=sys.stderr if issue.is_error else sys.stdout)
    if errors:
        _warn("config kept as-is; fix the errors and rerun `linteum config edit`")
        return 1
    if completed.returncode != 0:
        _warn(f"editor exited with status {completed.returncode}")
        return 1

    changes = service.reload()
    if not changes.changes:
        print("no changes")
        return 0
    for change in changes.changes:
        print(
            f"{change.key}: {_format_value(change.old)} -> "
            f"{_format_value(change.new)}  [{change.scope}]"
        )
    print(f"revision: {changes.revision or '<missing>'}")
    _print_apply(service.apply(changes))
    return 0


def _config_validate(service: ConfigService, args) -> int:
    issues = service.validate(effective=args.effective, strict=args.strict)
    errors = [issue for issue in issues if issue.is_error]
    if args.json:
        _json_dump({
            "ok": not errors,
            "path": service.path(),
            "revision": service.revision(),
            "effective": args.effective,
            "strict": args.strict,
            "issues": [issue.to_dict() for issue in issues],
        })
        return 1 if errors else 0
    if not issues:
        print("ok")
    else:
        for issue in issues:
            print(_issue_line(issue))
    return 1 if errors else 0


def _config_reload(service: ConfigService, args) -> int:
    changes = service.reload()
    if args.json:
        _json_dump({"ok": True, **changes.to_dict()})
        return 0
    print(f"revision: {changes.revision or '<missing>'}")
    if not changes.changes:
        print("no changes")
    else:
        for change in changes.changes:
            print(
                f"{change.key}: {_format_value(change.old)} -> "
                f"{_format_value(change.new)}  [{change.scope}]"
            )
    return 0


def _config_apply(service: ConfigService, args) -> int:
    changes = service.reload()
    result = service.apply(changes)
    if args.json:
        _json_dump({"ok": True, **result.to_dict()})
        return 0
    print(f"revision: {result.revision or '<missing>'}")
    _print_apply(result)
    if not (result.applied_live or result.requires_new_session or result.requires_restart):
        print("no changes")
    return 0


def _run_config(args) -> int:
    service = _build_service(args.config)
    json_mode = bool(getattr(args, "json", False))
    try:
        command = args.config_command
        if command == "init":
            return _config_init(service, args)
        if command == "path":
            return _config_path(service, args)
        if command == "list":
            return _config_list(service, args)
        if command == "get":
            return _config_get(service, args)
        if command == "set":
            return _config_set(service, args)
        if command == "unset":
            return _config_unset(service, args)
        if command == "edit":
            return _config_edit(service, args)
        if command == "validate":
            return _config_validate(service, args)
        if command == "reload":
            return _config_reload(service, args)
        if command == "apply":
            return _config_apply(service, args)
    except ConfigError as exc:
        return _config_error(exc, json_mode=json_mode)
    _warn(f"unknown config command: {args.config_command}")
    return 2


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------

def _setup(service: ConfigService, args) -> int:
    store = service.store
    exists = store.exists()
    current = store.load(create=False) if exists else default_config()
    llm = current.agent.llm
    tty = sys.stdin.isatty()

    if exists and not args.force:
        if not tty:
            _warn(f"{store.path} exists; use --force to update it")
            return 1
        answer = input(f"config exists at {store.path}; update it? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            return 0

    provider = args.provider
    if provider is None:
        if not tty:
            _warn("--provider is required when stdin is not a TTY")
            return 1
        provider = input(f"Provider [{llm.provider}]: ").strip() or llm.provider

    model = args.model
    if model is None:
        if not tty:
            _warn("--model is required when stdin is not a TTY")
            return 1
        model = input(f"Model [{llm.model}]: ").strip() or llm.model

    if args.base_url is not None:
        base_url = args.base_url or None
    elif provider != llm.provider:
        base_url = None
    else:
        base_url = llm.base_url

    needs_base = (
        provider not in PROVIDER_DEFAULTS or provider == "openai_compatible"
    )
    if needs_base and not base_url:
        if not tty:
            _warn(f"provider {provider!r} requires --base-url")
            return 1
        base_url = input("Base URL: ").strip() or None
        if not base_url:
            _warn(f"provider {provider!r} requires a base URL")
            return 1

    key = args.api_key
    if key is None and tty:
        env_name = PROVIDER_DEFAULTS.get(provider, {}).get("api_key_env")
        hint = f" (leave empty to use ${env_name})" if env_name else " (optional)"
        try:
            key = getpass.getpass(f"API key{hint}: ")
        except (EOFError, KeyboardInterrupt):
            key = ""
    if key == "":
        key = None

    if key is not None:
        new_key = key
    elif provider != llm.provider:
        new_key = None
    else:
        new_key = llm.api_key

    new_llm = replace(
        llm,
        provider=provider,
        model=model,
        api_key=new_key,
        base_url=base_url,
    )
    config = replace(current, agent=replace(current.agent, llm=new_llm))
    store.save(config)
    effective = service.effective(strict=False)

    if args.json:
        _json_dump({
            "ok": True,
            "path": service.path(),
            "revision": effective.revision,
            "provider": provider,
            "model": model,
            "api_key": "set" if new_key else "unset",
            "issues": [issue.to_dict() for issue in effective.issues],
        })
        return 0

    print(f"wrote {store.path}")
    print(f"provider: {provider}")
    print(f"model: {model}")
    if new_key:
        print("api_key: set")
    else:
        print("api_key: not set (the provider's env var will be used if available)")
    for issue in effective.issues:
        print(_issue_line(issue))
    print("next: linteum chat -w <workspace>")
    return 0


def _run_setup(args) -> int:
    service = _build_service(args.config)
    try:
        return _setup(service, args)
    except ConfigError as exc:
        return _config_error(exc, json_mode=args.json)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            return asyncio.run(_serve(args))
        if args.command == "chat":
            return asyncio.run(_chat(args))
        if args.command == "sessions":
            return asyncio.run(_sessions(args))
        if args.command == "config":
            return _run_config(args)
        if args.command == "setup":
            return _run_setup(args)
    except KeyboardInterrupt:
        return 130
    parser.error(f"unknown command: {args.command}")
    return 2


__all__ = ["main"]
