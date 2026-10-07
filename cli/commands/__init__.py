"""``cli/commands/*`` -- the **implementations** (handlers) of each subcommand.

They are lazily imported by ``main._forward_command`` when invoked (``importlib.import_module``), so:

- These modules are not loaded at startup (no AgentRunner construction / no heavy dependency import);
- ``patch("<module>.<attr>")`` can still intercept the real handler.

Each module exports a ``run_<group>(args) -> int``: when it returns an ``int``, ``main()`` uses it as
the process exit code.
"""

from __future__ import annotations
