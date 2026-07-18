def fizzbuzz(n: int) -> str:
    if n < 1:
        raise ValueError("n must be >= 1")
    if n % 15 == 0:
        return "FizzBuzz"
    if n % 3 == 0:
        return "Fizz"
    if n % 5 == 0:
        return "Buzz"
    return str(n)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        sys.stderr.write("Usage: fizzbuzz.py <number>\n")
        sys.exit(1)

    try:
        limit = int(sys.argv[1])
    except ValueError:
        sys.stderr.write(f"Error: argument must be an integer\n")
        sys.exit(1)

    if limit < 1:
        sys.stderr.write("Error: argument must be positive (>= 1)\n")
        sys.exit(1)

    for i in range(1, limit + 1):
        print(fizzbuzz(i))
