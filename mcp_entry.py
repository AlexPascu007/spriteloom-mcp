#!/usr/bin/env python3
"""Absolute-path entry point for the SpriteLoom stdio MCP server."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
os.chdir(REPO_ROOT)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from spriteloom_mcp.server import main  # noqa: E402


if __name__ == "__main__":
    asyncio.run(main())
