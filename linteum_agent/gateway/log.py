"""Gateway logging (§48).

The gateway reports lifecycle and failure paths through the standard
:mod:`logging` module under the ``linteum.gateway`` hierarchy.  It never
installs handlers, touches the root logger or changes levels: an embedding host
decides where records go (``linteum serve``/``chat`` and tests can attach their
own handlers).  Without a handler only ``warning`` and above are visible via the
stdlib last-resort handler, which is the desired default for a server.

Structured audit records are a separate channel: :class:`GatewayServer` and
:class:`SessionGateway` still call ``bus.emit_log`` for command audit and pump
errors.  These loggers cover what has no bus event — connection and session
lifecycle, queue overflows, lease transitions and transport failures.

Log records carry ids, counts, error classes and codes only; message text,
payloads and credentials never reach the log.
"""

from __future__ import annotations

import logging

#: Root of the gateway logger hierarchy.  Configure this name to capture the
#: whole package (``logging.getLogger("linteum.gateway").setLevel(...)``).
LOGGER_NAME = "linteum.gateway"


def get_logger(component: str) -> logging.Logger:
    """Return the module logger ``linteum.gateway.<component>``.

    The stdlib caches loggers by name, so this is cheap to call at import time.
    """
    return logging.getLogger(f"{LOGGER_NAME}.{component}")


__all__ = ["LOGGER_NAME", "get_logger"]
