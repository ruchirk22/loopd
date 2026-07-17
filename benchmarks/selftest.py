#!/usr/bin/env python3
"""Validate that each task's check.py is a FAIR judge — it must pass a correct reference
implementation and fail obviously-broken ones. No API, no network: pure local check.

    python3 benchmarks/selftest.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TASKS = ROOT / "tasks"

# --- correct reference implementations ---------------------------------------

ROMAN_GOOD = r'''
import sys
_VALUES = [(1000,"M"),(900,"CM"),(500,"D"),(400,"CD"),(100,"C"),(90,"XC"),
           (50,"L"),(40,"XL"),(10,"X"),(9,"IX"),(5,"V"),(4,"IV"),(1,"I")]
def to_roman(n):
    if not isinstance(n, int) or isinstance(n, bool) or not (1 <= n <= 3999):
        raise ValueError("out of range")
    out = []
    for v, s in _VALUES:
        while n >= v:
            out.append(s); n -= v
    return "".join(out)
def from_roman(s):
    if not isinstance(s, str) or not s:
        raise ValueError("empty")
    if to_roman_safe(s) is None:
        raise ValueError("malformed")
    total, i = 0, 0
    for v, sym in _VALUES:
        while s[i:i+len(sym)] == sym:
            total += v; i += len(sym)
    if i != len(s):
        raise ValueError("malformed")
    return total
def to_roman_safe(s):
    try:
        total, i = 0, 0
        for v, sym in _VALUES:
            while s[i:i+len(sym)] == sym:
                total += v; i += len(sym)
        if i != len(s) or total < 1 or total > 3999:
            return None
        if to_roman(total) != s:
            return None
        return total
    except Exception:
        return None
if __name__ == "__main__":
    cmd, val = sys.argv[1], sys.argv[2]
    try:
        print(to_roman(int(val)) if cmd == "to" else from_roman(val))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr); sys.exit(1)
'''

FIZZBUZZ_GOOD = r'''
import sys
def fizzbuzz(n):
    if n < 1:
        raise ValueError("n must be >= 1")
    if n % 15 == 0: return "FizzBuzz"
    if n % 3 == 0: return "Fizz"
    if n % 5 == 0: return "Buzz"
    return str(n)
if __name__ == "__main__":
    try:
        upto = int(sys.argv[1])
        if upto < 1: raise ValueError
    except (IndexError, ValueError):
        print("usage: fizzbuzz.py <positive int>", file=sys.stderr); sys.exit(1)
    for i in range(1, upto + 1):
        print(fizzbuzz(i))
'''

PRIMES_GOOD = r'''
import sys
def primes_up_to(n):
    if n < 2: return []
    sieve = bytearray([1]) * (n + 1)
    sieve[0] = sieve[1] = 0
    for i in range(2, int(n**0.5) + 1):
        if sieve[i]:
            sieve[i*i::i] = bytearray(len(sieve[i*i::i]))
    return [i for i in range(2, n + 1) if sieve[i]]
def is_prime(n):
    if n < 2: return False
    i = 2
    while i * i <= n:
        if n % i == 0: return False
        i += 1
    return True
def nth_prime(k):
    if k < 1: raise ValueError("k must be >= 1")
    limit = 20
    while True:
        ps = primes_up_to(limit)
        if len(ps) >= k: return ps[k-1]
        limit *= 2
if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "is": print(is_prime(int(sys.argv[2])))
    elif cmd == "nth": print(nth_prime(int(sys.argv[2])))
'''

# a deliberately broken variant per task (should FAIL the check)
ROMAN_BAD = "def to_roman(n):\n return 'X'\ndef from_roman(s):\n return 0\n"
FIZZBUZZ_BAD = "def fizzbuzz(n):\n return str(n)\n"           # never Fizz/Buzz
PRIMES_BAD = ("def is_prime(n):\n return True\n"
              "def primes_up_to(n):\n return []\n"
              "def nth_prime(k):\n return 0\n")

CASES = {
    "roman": ("roman.py", ROMAN_GOOD, ROMAN_BAD),
    "fizzbuzz": ("fizzbuzz.py", FIZZBUZZ_GOOD, FIZZBUZZ_BAD),
    "primes": ("primes.py", PRIMES_GOOD, PRIMES_BAD),
}


def run_check(task, workdir):
    p = subprocess.run([sys.executable, str(TASKS / task / "check.py"), str(workdir)],
                       capture_output=True, text=True, timeout=120)
    return p.returncode == 0, (p.stdout + p.stderr).strip().splitlines()[-1:]


def main():
    failures = 0
    for task, (fname, good, bad) in CASES.items():
        for label, src, expect_pass in (("good", good, True), ("bad", bad, False)):
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / fname).write_text(src)
                ok, tail = run_check(task, Path(d))
                verdict = "PASS" if ok else "FAIL"
                good_result = (ok == expect_pass)
                mark = "✓" if good_result else "✗ UNEXPECTED"
                print(f"  {mark} {task}/{label}: check returned {verdict} "
                      f"(expected {'PASS' if expect_pass else 'FAIL'})  {tail}")
                if not good_result:
                    failures += 1
    print()
    if failures:
        print(f"SELFTEST FAILED: {failures} check(s) are not fair judges.")
        return 1
    print("SELFTEST OK: every check passes a correct impl and fails a broken one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
