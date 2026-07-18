"""Tests for the Roman numeral converter."""

import unittest
from roman import to_roman, from_roman


class TestToRoman(unittest.TestCase):
    """Test to_roman() function."""

    def test_basic_numerals(self):
        """Test conversion of basic values."""
        self.assertEqual(to_roman(1), "I")
        self.assertEqual(to_roman(5), "V")
        self.assertEqual(to_roman(10), "X")
        self.assertEqual(to_roman(50), "L")
        self.assertEqual(to_roman(100), "C")
        self.assertEqual(to_roman(500), "D")
        self.assertEqual(to_roman(1000), "M")

    def test_subtractive_notation(self):
        """Test conversion with subtractive notation."""
        self.assertEqual(to_roman(4), "IV")
        self.assertEqual(to_roman(9), "IX")
        self.assertEqual(to_roman(40), "XL")
        self.assertEqual(to_roman(90), "XC")
        self.assertEqual(to_roman(400), "CD")
        self.assertEqual(to_roman(900), "CM")

    def test_known_conversions(self):
        """Test known conversions."""
        self.assertEqual(to_roman(2024), "MMXXIV")
        self.assertEqual(to_roman(1994), "MCMXCIV")
        self.assertEqual(to_roman(3999), "MMMCMXCIX")

    def test_out_of_range_values(self):
        """Test that out-of-range values raise ValueError."""
        with self.assertRaises(ValueError):
            to_roman(0)
        with self.assertRaises(ValueError):
            to_roman(-5)
        with self.assertRaises(ValueError):
            to_roman(4000)
        with self.assertRaises(ValueError):
            to_roman(5000)

    def test_non_integer_input(self):
        """Test that non-integer input raises ValueError."""
        with self.assertRaises(ValueError):
            to_roman(3.5)
        with self.assertRaises(ValueError):
            to_roman("42")


class TestFromRoman(unittest.TestCase):
    """Test from_roman() function."""

    def test_basic_numerals(self):
        """Test parsing basic values."""
        self.assertEqual(from_roman("I"), 1)
        self.assertEqual(from_roman("V"), 5)
        self.assertEqual(from_roman("X"), 10)
        self.assertEqual(from_roman("L"), 50)
        self.assertEqual(from_roman("C"), 100)
        self.assertEqual(from_roman("D"), 500)
        self.assertEqual(from_roman("M"), 1000)

    def test_subtractive_notation(self):
        """Test parsing with subtractive notation."""
        self.assertEqual(from_roman("IV"), 4)
        self.assertEqual(from_roman("IX"), 9)
        self.assertEqual(from_roman("XL"), 40)
        self.assertEqual(from_roman("XC"), 90)
        self.assertEqual(from_roman("CD"), 400)
        self.assertEqual(from_roman("CM"), 900)

    def test_known_conversions(self):
        """Test known conversions."""
        self.assertEqual(from_roman("MMXXIV"), 2024)
        self.assertEqual(from_roman("MCMXCIV"), 1994)
        self.assertEqual(from_roman("MMMCMXCIX"), 3999)

    def test_invalid_characters(self):
        """Test that invalid characters raise ValueError."""
        with self.assertRaises(ValueError):
            from_roman("abc")
        with self.assertRaises(ValueError):
            from_roman("MCMXC1V")

    def test_empty_string(self):
        """Test that empty string raises ValueError."""
        with self.assertRaises(ValueError):
            from_roman("")

    def test_invalid_subtractive_notation(self):
        """Test that invalid subtractive notation raises ValueError."""
        with self.assertRaises(ValueError):
            from_roman("IIII")
        with self.assertRaises(ValueError):
            from_roman("IL")


class TestRoundTrip(unittest.TestCase):
    """Test round-trip conversion (to_roman -> from_roman)."""

    def test_round_trip_full_range(self):
        """Test that round-trip works for all valid values 1-3999."""
        for n in range(1, 4000):
            roman = to_roman(n)
            back = from_roman(roman)
            self.assertEqual(n, back, f"Round-trip failed for {n}: to_roman={roman}, from_roman={back}")

    def test_round_trip_samples(self):
        """Test round-trip for specific sample values."""
        test_values = [1, 4, 9, 27, 40, 49, 90, 123, 400, 500, 900, 1994, 2024, 3999]
        for n in test_values:
            self.assertEqual(n, from_roman(to_roman(n)))


if __name__ == '__main__':
    unittest.main()
