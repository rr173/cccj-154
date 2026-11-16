"""Exact-rational arithmetic and a few invariants of the storage layer."""

from __future__ import annotations

import unittest
from fractions import Fraction

from unitpipe.rational import Q, parse_decimal


class TestRational(unittest.TestCase):
    def test_normalisation(self):
        self.assertEqual(Q(2, 4), Q(1, 2))
        self.assertEqual(Q(-2, -4), Q(1, 2))
        self.assertEqual(Q(2, -4), Q(-1, 2))
        self.assertEqual(Q(0, 5).str(), "0")
        self.assertEqual(Q(6, 3).str(), "2")

    def test_arithmetic_matches_fraction(self):
        a, b = Q(2, 3), Q(5, 7)
        self.assertEqual((a + b).fraction(), Fraction(2, 3) + Fraction(5, 7))
        self.assertEqual((a - b).fraction(), Fraction(2, 3) - Fraction(5, 7))
        self.assertEqual((a * b).fraction(), Fraction(2, 3) * Fraction(5, 7))
        self.assertEqual((a / b).fraction(), Fraction(2, 3) / Fraction(5, 7))
        self.assertEqual((a ** -3).fraction(), Fraction(2, 3) ** -3)
        self.assertEqual((a ** 0).fraction(), Fraction(1))

    def test_mixed_int_and_negation(self):
        a = Q(1, 2)
        self.assertEqual((a + 1).str(), "3/2")
        self.assertEqual((1 + a).str(), "3/2")
        self.assertEqual((2 * a).str(), "1")
        self.assertEqual((2 / a).str(), "4")
        self.assertEqual((-a).str(), "-1/2")
        self.assertEqual((1 - a).str(), "1/2")

    def test_decimal_parsing_exact(self):
        cases = {
            "0.1": Fraction(1, 10),
            "273.15": Fraction(27315, 100),
            "-459.67": Fraction(-45967, 100),
            "5.": Fraction(5),
            ".25": Fraction(1, 4),
            "+3.50": Fraction(7, 2),
            "0": Fraction(0),
            "00012.3400": Fraction(1234, 100),
            "0.000000000000000001": Fraction(1, 10 ** 18),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_decimal(text).fraction(), expected)

    def test_decimal_never_uses_float(self):
        # A value that is not representable in binary floating point must stay
        # exact through a round trip.
        value = "0.30000000000000004"
        q = parse_decimal(value)
        self.assertEqual(q.fraction(), Fraction(30000000000000004, 10 ** 17))
        self.assertNotEqual(q.fraction(), Fraction(0.1) + Fraction(0.2))

    def test_decimal_rejects_non_decimal(self):
        for text in ["", " ", "1e3", "NaN", "inf", "0x10", "1,2", "--1",
                     "1.2.3", " 1", "1 ", "1 2", "1/2", "３", "١.٥"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_decimal(text))

    def test_zero_division(self):
        with self.assertRaises(ZeroDivisionError):
            Q(1, 2) / Q(0, 1)
        with self.assertRaises(ZeroDivisionError):
            Q(0, 1) ** -1

    def test_atom_parenthesises_negatives(self):
        self.assertEqual(Q(-1, 2).atom(), "(-1/2)")
        self.assertEqual(Q(1, 2).atom(), "(1/2)")
        self.assertEqual(Q(3, 1).atom(), "3")
        self.assertEqual(Q(-3, 1).atom(), "(-3)")


if __name__ == "__main__":
    unittest.main()
