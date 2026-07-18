"""Enables `python3 -m orchestrator` as an alias for the `loopd` command."""
import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
