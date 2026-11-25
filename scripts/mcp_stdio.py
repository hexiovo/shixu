from __future__ import annotations

import asyncio
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.mcp.server import main  # noqa: E402


if __name__ == "__main__":
    asyncio.run(main())
