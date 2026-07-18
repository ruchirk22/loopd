#!/usr/bin/env python3
"""Independent objective check for the roman task — owned by the harness, not the agent.
Usage: python3 check.py <repo_dir>  (exit 0 = deliverable is correct)."""
import importlib.util
import subprocess
import sys
from pathlib import Path


def fail(m):
    print(f"CHECK FAIL: {m}")
    sys.exit(1)


def load(repo, name):
    path = repo / f"{name}.py"
    if not path.exists():
        fail(f"{name}.py missing")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # register before exec (matches a normal `import`)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        fail(f"importing {name}.py raised {type(e).__name__}: {e}")
    return mod


def main():
    repo = Path(sys.argv[1]).resolve()
    m = load(repo, "roman")
    for fn in ("to_roman", "from_roman"):
        if not hasattr(m, fn):
            fail(f"roman.{fn} missing")

    cases = {1: "I", 4: "IV", 9: "IX", 40: "XL", 90: "XC", 400: "CD", 900: "CM",
             1994: "MCMXCIV", 2024: "MMXXIV", 3999: "MMMCMXCIX"}
    for n, r in cases.items():
        if m.to_roman(n) != r:
            fail(f"to_roman({n}) = {m.to_roman(n)!r}, want {r!r}")
        if m.from_roman(r) != n:
            fail(f"from_roman({r!r}) = {m.from_roman(r)!r}, want {n}")

    for n in range(1, 4000):  # full round-trip
        if m.from_roman(m.to_roman(n)) != n:
            fail(f"round-trip failed at {n}")

    for bad in (0, 4000, -5):
        try:
            m.to_roman(bad)
            fail(f"to_roman({bad}) should raise ValueError")
        except ValueError:
            pass
    for bad in ("", "abc", "IIII", "IL"):
        try:
            m.from_roman(bad)
            fail(f"from_roman({bad!r}) should raise ValueError")
        except ValueError:
            pass

    cli = repo / "roman.py"
    for args, want in ((["to", "2024"], "MMXXIV"), (["from", "MMXXIV"], "2024")):
        out = subprocess.run([sys.executable, str(cli), *args], capture_output=True, text=True)
        if out.stdout.strip() != want:
            fail(f"CLI `{' '.join(args)}` printed {out.stdout.strip()!r}, want {want!r}")

    print("CHECK OK: roman")
    sys.exit(0)


if __name__ == "__main__":
    main()
