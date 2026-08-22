"""Vercel serverless entrypoint.

vercel.json rewrites every route here. The repo root holds app.py, pipeline.py
and insights.py, so it has to be importable from inside api/.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app  # noqa: E402 - the path setup above must run first

__all__ = ["app"]
