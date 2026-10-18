"""Lightweight CLI (Phase 3: ``serve``; chat/REPL arrive with Phase 4).

``linteum serve`` is a *consumer* of the gateway, exactly like the TUI/Web
front-end: it builds ``AgentHost``, starts a transport and hands control to
``GatewayServer``.  It never touches the bus directly.
"""

from .main import main

__all__ = ["main"]
