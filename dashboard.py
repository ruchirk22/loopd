#!/usr/bin/env python3
"""Low-level entrypoint for the loopd web dashboard. Prefer `loopd ui`; this is the
equivalent direct entry.

    loopd ui                           # the usual way — opens on the current project
    python3 dashboard.py --repo ../my-app --port 9000   # equivalent, low-level

Local tool: binds to 127.0.0.1 by default and spawns the engine (python -m orchestrator.run)
— do not expose it.
"""
from orchestrator.dashboard import main

if __name__ == "__main__":
    raise SystemExit(main())
