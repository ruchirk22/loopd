#!/usr/bin/env python3
"""Entrypoint for the loopd web dashboard. Launch and watch runs from a browser.

    python3 dashboard.py --repo ../my-app      # default target repo, opens on :8787
    python3 dashboard.py --port 9000

Local tool: binds to 127.0.0.1 by default and spawns run.py — do not expose it.
"""
from orchestrator.dashboard import main

if __name__ == "__main__":
    raise SystemExit(main())
