#!/usr/bin/env python3
"""Independent objective check for the primes task."""
import importlib.util
import subprocess
import sys
import time
from pathlib import Path


def fail(m):
    print(f"CHECK FAIL: {m}")
    sys.exit(1)


def main():
    repo = Path(sys.argv[1]).resolve()
    path = repo / "primes.py"
    if not path.exists():
        fail("primes.py missing")
    spec = importlib.util.spec_from_file_location("primes", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        fail(f"importing primes.py raised {type(e).__name__}: {e}")
    for fn in ("is_prime", "primes_up_to", "nth_prime"):
        if not hasattr(mod, fn):
            fail(f"primes.{fn} missing")

    primes50 = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47]
    for n in range(-3, 51):
        want = n in primes50
        if mod.is_prime(n) != want:
            fail(f"is_prime({n}) = {mod.is_prime(n)}, want {want}")

    if mod.primes_up_to(1) != []:
        fail("primes_up_to(1) should be []")
    if mod.primes_up_to(2) != [2]:
        fail("primes_up_to(2) should be [2]")
    if mod.primes_up_to(30) != [2, 3, 5, 7, 11, 13, 17, 19, 23, 29]:
        fail(f"primes_up_to(30) = {mod.primes_up_to(30)}")

    for k, want in ((1, 2), (10, 29), (25, 97), (100, 541)):
        if mod.nth_prime(k) != want:
            fail(f"nth_prime({k}) = {mod.nth_prime(k)}, want {want}")
    try:
        mod.nth_prime(0)
        fail("nth_prime(0) should raise ValueError")
    except ValueError:
        pass

    t0 = time.time()
    got = mod.primes_up_to(100000)
    if time.time() - t0 > 2.0:
        fail("primes_up_to(100000) too slow (>2s) — use a sieve")
    if len(got) != 9592:  # known count of primes below 100000
        fail(f"primes_up_to(100000) has {len(got)} primes, want 9592")

    out = subprocess.run([sys.executable, str(path), "nth", "25"], capture_output=True, text=True)
    if out.stdout.strip() != "97":
        fail(f"CLI `nth 25` printed {out.stdout.strip()!r}, want '97'")

    print("CHECK OK: primes")
    sys.exit(0)


if __name__ == "__main__":
    main()
