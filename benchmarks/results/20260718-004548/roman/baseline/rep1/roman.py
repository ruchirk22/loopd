"""Convert between integers (1-3999) and Roman numerals."""


def to_roman(n: int) -> str:
    """Convert an integer to a Roman numeral string.

    Args:
        n: An integer in the range 1-3999.

    Returns:
        A Roman numeral string in standard subtractive form.

    Raises:
        ValueError: If n is outside the range 1-3999.
    """
    if not isinstance(n, int) or n < 1 or n > 3999:
        raise ValueError(f"Number must be an integer between 1 and 3999, got {n}")

    val = [
        1000, 900, 500, 400,
        100, 90, 50, 40,
        10, 9, 5, 4,
        1
    ]
    syms = [
        "M", "CM", "D", "CD",
        "C", "XC", "L", "XL",
        "X", "IX", "V", "IV",
        "I"
    ]
    roman_num = ''
    i = 0
    while n > 0:
        for _ in range(n // val[i]):
            roman_num += syms[i]
            n -= val[i]
        i += 1
    return roman_num


def from_roman(s: str) -> int:
    """Convert a Roman numeral string to an integer.

    Args:
        s: A Roman numeral string in standard form.

    Returns:
        An integer in the range 1-3999.

    Raises:
        ValueError: If the string is empty, contains invalid characters,
                    or uses invalid subtractive notation.
    """
    if not s:
        raise ValueError("Empty string is not a valid Roman numeral")

    roman_val = {
        'I': 1,
        'V': 5,
        'X': 10,
        'L': 50,
        'C': 100,
        'D': 500,
        'M': 1000
    }

    for char in s:
        if char not in roman_val:
            raise ValueError(f"Invalid character '{char}' in Roman numeral")

    total = 0
    prev_val = 0

    for char in reversed(s):
        val = roman_val[char]

        if val < prev_val:
            total -= val
        else:
            total += val

        prev_val = val

    if total < 1 or total > 3999:
        raise ValueError(f"Roman numeral '{s}' is outside valid range (1-3999)")

    if to_roman(total) != s:
        raise ValueError(f"'{s}' is not a valid Roman numeral")

    return total


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python3 roman.py [to|from] <value>", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]
    value = sys.argv[2]

    try:
        if command == "to":
            n = int(value)
            print(to_roman(n))
        elif command == "from":
            print(from_roman(value))
        else:
            print(f"Unknown command '{command}'. Use 'to' or 'from'.", file=sys.stderr)
            sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
