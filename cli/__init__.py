"""The linteum_agent command-line front-end package.

Public entry points (the two are equivalent)::

    python -m cli.main <command> ...
    python -m cli <command> ...

Architecture (mirroring hermes' Builder + dependency injection):

- ``main.py``: parses ``argv``, builds the full argparse tree, and dispatches via ``args.func(args)``;
- ``subcommands/<group>.py``: **only** provides ``build_<group>_parser(subparsers, ...)``; ``main``
  injects the handler, and it never imports ``main`` (to avoid circular imports);
- ``commands/<group>.py``: the implementations of each subcommand, lazily imported by
  ``_forward_command`` **when invoked** (``importlib.import_module``), balancing startup speed with
  monkeypatch interceptability.
"""

from __future__ import annotations

#: The command-line program name (argparse's ``prog``, also the prefix of ``--help`` / usage lines).
PROG = "linteum"

#: CLI version number.
__version__ = "0.1.0"

__all__ = ["PROG", "__version__"]
