"""``python -m cronaudit`` and the ``cron-audit`` console script."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
