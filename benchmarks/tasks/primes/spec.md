# Task: Prime-number utilities (Python, stdlib only)

## Objective
Create `primes.py` — a small, correct, well-tested prime-number utility module with a CLI.

## Hard constraints
- Python 3 standard library ONLY. No third-party packages, no `pip install`, no network.
- Tests MUST run with `python3 -m unittest discover -q` (do NOT use pytest).
- Must be efficient enough that `primes_up_to(100000)` returns in well under a second
  (use a sieve, not trial division per number).

## What to build
1. `primes.py` with:
   - `is_prime(n: int) -> bool` — correct for all integers (n < 2 is not prime).
   - `primes_up_to(n: int) -> list[int]` — all primes ≤ n, ascending (empty for n < 2).
   - `nth_prime(k: int) -> int` — the k-th prime, 1-indexed (`nth_prime(1) == 2`);
     raise `ValueError` for k < 1.
2. A CLI guarded by `if __name__ == "__main__":`:
   - `python3 primes.py is 97` prints `True`; `python3 primes.py is 98` prints `False`.
   - `python3 primes.py nth 10` prints `29`.
3. `test_primes.py` (stdlib `unittest`) covering known primes/non-primes, `primes_up_to`
   boundaries (0, 1, 2, 10, 30), `nth_prime` values, and the `ValueError`.

## Definition of done
- `python3 -m unittest discover -q` exits 0.
- `python3 -c "import primes; assert primes.is_prime(97); assert not primes.is_prime(1); assert primes.primes_up_to(10)==[2,3,5,7]; assert primes.nth_prime(10)==29"` exits 0.
- `python3 primes.py nth 25` prints exactly `97`.

## Out of scope
No packaging, no README, no third-party dependencies.
