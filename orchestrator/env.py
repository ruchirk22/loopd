"""Tiny stdlib .env loader — so you can `cp .env.example .env`, drop your key in, and run,
instead of `export`ing every shell. No dependencies (python-dotenv not required)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def load_dotenv(path: Optional[Path] = None) -> bool:
    """Load KEY=VALUE lines from `.env` into os.environ WITHOUT overriding variables that
    are already set (an explicit `export` still wins). Defaults to `.env` in the repo root.
    Returns True if a file was found and read."""
    if path is None:
        path = Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return False
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line[len("export "):].strip() if line.startswith("export ") else line
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]  # strip one layer of surrounding quotes
        if key and key not in os.environ:
            os.environ[key] = val
    return True
