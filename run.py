#!/usr/bin/env python3
"""Dev-convenience shim for a source checkout. The engine also runs as
`python -m orchestrator.run` (what the installed package and the dashboard use), and most
people should just use the `loopd` command.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from orchestrator.run import main  # noqa: E402

if __name__ == "__main__":
    main()
