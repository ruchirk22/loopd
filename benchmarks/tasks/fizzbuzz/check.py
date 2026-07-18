#!/usr/bin/env python3
"""Independent objective check for the fizzbuzz task."""
import importlib.util
import subprocess
import sys
from pathlib import Path


def fail(m):
    print(f"CHECK FAIL: {m}")
    sys.exit(1)


def main():
    repo = Path(sys.argv[1]).resolve()
    path = repo / "fizzbuzz.py"
    if not path.exists():
        fail("fizzbuzz.py missing")
    spec = importlib.util.spec_from_file_location("fizzbuzz", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fizzbuzz"] = mod  # register before exec (matches a normal `import`)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        fail(f"importing fizzbuzz.py raised {type(e).__name__}: {e}")
    if not hasattr(mod, "fizzbuzz"):
        fail("fizzbuzz.fizzbuzz missing")

    cases = {1: "1", 2: "2", 3: "Fizz", 5: "Buzz", 7: "7", 9: "Fizz",
             10: "Buzz", 15: "FizzBuzz", 30: "FizzBuzz", 45: "FizzBuzz", 98: "98"}
    for n, want in cases.items():
        if mod.fizzbuzz(n) != want:
            fail(f"fizzbuzz({n}) = {mod.fizzbuzz(n)!r}, want {want!r}")
    try:
        mod.fizzbuzz(0)
        fail("fizzbuzz(0) should raise ValueError")
    except ValueError:
        pass

    out = subprocess.run([sys.executable, str(path), "5"], capture_output=True, text=True)
    got = [ln for ln in out.stdout.strip().splitlines()]
    if got != ["1", "2", "Fizz", "4", "Buzz"]:
        fail(f"CLI `5` printed {got!r}, want ['1','2','Fizz','4','Buzz']")

    print("CHECK OK: fizzbuzz")
    sys.exit(0)


if __name__ == "__main__":
    main()
