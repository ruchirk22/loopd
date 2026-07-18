def is_prime(n: int) -> bool:
    """Check if n is prime. Returns False for n < 2."""
    if n < 2:
        return False
    if n == 2:
        return True
    if n % 2 == 0:
        return False
    for i in range(3, int(n**0.5) + 1, 2):
        if n % i == 0:
            return False
    return True


def primes_up_to(n: int) -> list[int]:
    """Return all primes <= n in ascending order."""
    if n < 2:
        return []

    # Sieve of Eratosthenes
    sieve = [True] * (n + 1)
    sieve[0] = sieve[1] = False

    for i in range(2, int(n**0.5) + 1):
        if sieve[i]:
            for j in range(i*i, n + 1, i):
                sieve[j] = False

    return [i for i in range(2, n + 1) if sieve[i]]


def nth_prime(k: int) -> int:
    """Return the k-th prime (1-indexed). Raises ValueError if k < 1."""
    if k < 1:
        raise ValueError("k must be >= 1")

    if k == 1:
        return 2

    # Upper bound for the n-th prime (Rosser's theorem)
    # For n >= 6: p_n < n * (ln(n) + ln(ln(n)))
    import math
    if k < 6:
        limit = 15
    else:
        limit = int(k * (math.log(k) + math.log(math.log(k))) * 1.3)

    primes = primes_up_to(limit)
    while len(primes) < k:
        limit *= 2
        primes = primes_up_to(limit)

    return primes[k - 1]


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 primes.py [is|nth] <number>")
        sys.exit(1)

    command = sys.argv[1]

    if command == "is":
        if len(sys.argv) < 3:
            print("Usage: python3 primes.py is <number>")
            sys.exit(1)
        n = int(sys.argv[2])
        print(is_prime(n))
    elif command == "nth":
        if len(sys.argv) < 3:
            print("Usage: python3 primes.py nth <k>")
            sys.exit(1)
        k = int(sys.argv[2])
        print(nth_prime(k))
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)
