# Task: FizzBuzz library + CLI (Python, stdlib only)

## Objective
Create `fizzbuzz.py` — a small, tested FizzBuzz implementation with a library function
and a command-line interface.

## Hard constraints
- Python 3 standard library ONLY. No third-party packages, no `pip install`, no network.
- Tests MUST run with `python3 -m unittest discover -q` (do NOT use pytest).
- Everything lives in this repository.

## What to build
1. `fizzbuzz.py` with a pure function `fizzbuzz(n: int) -> str`:
   - multiples of 15 -> `"FizzBuzz"`, of 3 -> `"Fizz"`, of 5 -> `"Buzz"`, else `str(n)`.
   - raise `ValueError` for `n < 1`.
2. A CLI guarded by `if __name__ == "__main__":`:
   - `python3 fizzbuzz.py 15` prints lines 1 through 15, one result per line
     (so the 15th line is `FizzBuzz`, the 3rd is `Fizz`, the 5th is `Buzz`).
   - a non-positive or non-integer argument exits non-zero with an error on stderr.
3. `test_fizzbuzz.py` (stdlib `unittest`) covering the classic values and the ValueError.

## Definition of done
- `python3 -m unittest discover -q` exits 0.
- `python3 -c "import fizzbuzz; assert fizzbuzz(15)=='FizzBuzz'; assert fizzbuzz(3)=='Fizz'; assert fizzbuzz(5)=='Buzz'; assert fizzbuzz(7)=='7'"` exits 0.
- `python3 fizzbuzz.py 5` prints exactly the five lines: `1`, `2`, `Fizz`, `4`, `Buzz`.

## Out of scope
No packaging, no README, no third-party dependencies.
