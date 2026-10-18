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
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from ..config.service import ConfigService
from ..config.store import ConfigStore
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
    return parser


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
    except KeyboardInterrupt:
        return 130
    parser.error(f"unknown command: {args.command}")
    return 2


__all__ = ["main"]
