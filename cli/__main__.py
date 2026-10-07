"""``python -m cli`` entry point: forwards to ``cli.main.main``."""

from __future__ import annotations

import sys

from cli.main import main

if __name__ == "__main__":
    sys.exit(main())
