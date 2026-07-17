# Task: Roman numeral converter (Python, stdlib only)

## Objective
Create a small, well-tested Python module `roman.py` that converts between integers and
Roman numerals, plus a command-line interface. It must be correct, self-contained, and
fully verified by the project's own tests.

## Hard constraints (respect these exactly)
- Python 3 standard library ONLY. No third-party packages, no `pip install`, no network.
- Tests MUST run with the stdlib test runner: `python3 -m unittest discover -q` (do NOT
  use pytest — it is not installed and must not be added).
- Everything lives in this repository; do not touch anything outside it.

## What to build
1. `roman.py` with two pure functions:
   - `to_roman(n: int) -> str` — convert an integer in the range 1..3999 to its Roman
     numeral (uppercase, standard subtractive form, e.g. 4 -> "IV", 1994 -> "MCMXCIV").
   - `from_roman(s: str) -> int` — parse a valid Roman numeral back to its integer.
   - Both must raise `ValueError` on invalid input (out-of-range integers; empty or
     malformed numeral strings).
2. A CLI in the same file guarded by `if __name__ == "__main__":` supporting:
   - `python3 roman.py to 1994`   -> prints `MCMXCIV`
   - `python3 roman.py from MCMXCIV` -> prints `1994`
   - invalid usage or invalid values exit non-zero with a short error message on stderr.
3. A test module `test_roman.py` (stdlib `unittest`) covering:
   - known conversions in both directions (at least 1, 4, 9, 40, 90, 400, 900, 2024, 3999),
   - a round-trip property (`from_roman(to_roman(n)) == n`) across the full 1..3999 range,
   - `ValueError` on out-of-range ints (0, 4000, -5) and on malformed numerals ("", "IIII",
     "IL", "abc").

## Definition of done (all must hold)
- `python3 -m unittest discover -q` exits 0 with all tests passing.
- `python3 -c "import roman; assert roman.to_roman(1994)=='MCMXCIV'; assert roman.from_roman('IV')==4"`
  exits 0.
- `python3 roman.py to 2024` prints exactly `MMXXIV`.
- `python3 roman.py from MMXXIV` prints exactly `2024`.
- The round-trip test covers the entire 1..3999 range.

## Out of scope
- No packaging (`setup.py`/`pyproject.toml`), no README, no external dependencies,
  no support for numbers outside 1..3999.
