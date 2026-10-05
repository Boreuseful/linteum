"""``python -m cli`` 入口：转发到 ``cli.main.main``。"""

from __future__ import annotations

import sys

from cli.main import main

if __name__ == "__main__":
    sys.exit(main())
