import unittest
import primes


class TestIsPrime(unittest.TestCase):
    """Test the is_prime function."""

    def test_small_non_primes(self):
        """Test numbers < 2 and even non-primes."""
        self.assertFalse(primes.is_prime(-1))
        self.assertFalse(primes.is_prime(0))
        self.assertFalse(primes.is_prime(1))
        self.assertFalse(primes.is_prime(4))
        self.assertFalse(primes.is_prime(6))
        self.assertFalse(primes.is_prime(8))

    def test_small_primes(self):
        """Test small known primes."""
        for p in [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31]:
            self.assertTrue(primes.is_prime(p), f"{p} should be prime")

    def test_larger_primes(self):
        """Test larger known primes."""
        for p in [97, 101, 239, 997]:
            self.assertTrue(primes.is_prime(p), f"{p} should be prime")

    def test_larger_non_primes(self):
        """Test larger non-primes."""
        for n in [4, 6, 9, 15, 21, 25, 27, 100, 121]:
            self.assertFalse(primes.is_prime(n), f"{n} should not be prime")

    def test_two(self):
        """Test edge case of 2."""
        self.assertTrue(primes.is_prime(2))

    def test_odd_composites(self):
        """Test odd composite numbers."""
        for n in [9, 15, 21, 25, 27, 33, 35, 39, 45, 49]:
            self.assertFalse(primes.is_prime(n))


class TestPrimesUpTo(unittest.TestCase):
    """Test the primes_up_to function."""

    def test_empty_results(self):
        """Test when n < 2."""
        self.assertEqual(primes.primes_up_to(-5), [])
        self.assertEqual(primes.primes_up_to(0), [])
        self.assertEqual(primes.primes_up_to(1), [])

    def test_two(self):
        """Test n == 2."""
        self.assertEqual(primes.primes_up_to(2), [2])

    def test_up_to_10(self):
        """Test n == 10."""
        self.assertEqual(primes.primes_up_to(10), [2, 3, 5, 7])

    def test_up_to_30(self):
        """Test n == 30."""
        expected = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29]
        self.assertEqual(primes.primes_up_to(30), expected)

    def test_up_to_100(self):
        """Test n == 100."""
        expected = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47,
                    53, 59, 61, 67, 71, 73, 79, 83, 89, 97]
        self.assertEqual(primes.primes_up_to(100), expected)

    def test_ascending_order(self):
        """Test that results are in ascending order."""
        result = primes.primes_up_to(50)
        self.assertEqual(result, sorted(result))

    def test_all_results_are_prime(self):
        """Verify all returned values are indeed prime."""
        for p in primes.primes_up_to(100):
            self.assertTrue(primes.is_prime(p))

    def test_large_input(self):
        """Test that primes_up_to is efficient for large inputs."""
        result = primes.primes_up_to(100000)
        self.assertGreater(len(result), 9000)
        self.assertLess(len(result), 10000)
        self.assertEqual(result[0], 2)
        self.assertEqual(result[-1], 99991)


class TestNthPrime(unittest.TestCase):
    """Test the nth_prime function."""

    def test_first_prime(self):
        """Test the 1st prime."""
        self.assertEqual(primes.nth_prime(1), 2)

    def test_small_nth_primes(self):
        """Test small values of k."""
        expected = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29]
        for k, expected_prime in enumerate(expected, start=1):
            self.assertEqual(primes.nth_prime(k), expected_prime)

    def test_tenth_prime(self):
        """Test the 10th prime."""
        self.assertEqual(primes.nth_prime(10), 29)

    def test_25th_prime(self):
        """Test the 25th prime."""
        self.assertEqual(primes.nth_prime(25), 97)

    def test_100th_prime(self):
        """Test the 100th prime."""
        self.assertEqual(primes.nth_prime(100), 541)

    def test_invalid_k_zero(self):
        """Test that k=0 raises ValueError."""
        with self.assertRaises(ValueError):
            primes.nth_prime(0)

    def test_invalid_k_negative(self):
        """Test that k<0 raises ValueError."""
        with self.assertRaises(ValueError):
            primes.nth_prime(-1)
        with self.assertRaises(ValueError):
            primes.nth_prime(-10)


if __name__ == "__main__":
    unittest.main()
