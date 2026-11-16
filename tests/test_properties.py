"""Independent rational-property cross-checks.

These do not know the product's unit table: they only assert algebraic
properties of exact rational affine maps that any correct conversion must
have.  A separate, deliberately tiny evaluator written here computes expected
values for a handful of compound expressions from the hand table.
"""

from __future__ import annotations

import unittest
from fractions import Fraction

from support import F, engine_with_vendor


def tiny_eval(text: str) -> Fraction:
    """A minimal handwritten evaluator for the cases used below.

    It supports only products of unprefixed atoms with integer exponents and
    is intentionally independent of unitpipe's parser.
    """
    scale = {
        "m": Fraction(1),
        "s": Fraction(1),
        "g": Fraction(1),
        "K": Fraction(1),
        "L": Fraction(1, 1000),
        "mg": Fraction(1, 1000),
        "km": Fraction(1000),
        "mm": Fraction(1, 1000),
        "cm": Fraction(1, 100),
        "kg": Fraction(1000),
        "uL": Fraction(1, 1000000000),
        "mL": Fraction(1, 1000000),
    }
    atoms = {
        "m": ("length", 1),
        "s": ("time", 1),
        "g": ("mass", 1),
        "K": ("temperature", 1),
        "L": ("length", 3),
        "mg": ("mass", 1),
        "km": ("length", 1),
        "mm": ("length", 1),
        "cm": ("length", 1),
        "kg": ("mass", 1),
        "uL": ("length", 3),
        "mL": ("length", 3),
    }
    # Hand-written cases only; parse failures are meant to be loud.
    table = {
        "m2": (Fraction(1), {"length": 2}),
        "km2": (Fraction(10 ** 6), {"length": 2}),
        "km3": (Fraction(10 ** 9), {"length": 3}),
        "cm3": (Fraction(1, 10 ** 6), {"length": 3}),
        "m2/s2": (Fraction(1), {"length": 2, "time": -2}),
        "(m/s)2": (Fraction(1), {"length": 2, "time": -2}),
        "mg/L": (Fraction(1), {"length": -3, "mass": 1}),
        "kg/m3": (Fraction(1000), {"length": -3, "mass": 1}),
        "mm/s": (Fraction(1, 1000), {"length": 1, "time": -1}),
        "km/s": (Fraction(1000), {"length": 1, "time": -1}),
        "m(-2)": (Fraction(1), {"length": -2}),
        "km(-2)": (Fraction(1, 10 ** 6), {"length": -2}),
        "L/s": (Fraction(1, 1000), {"length": 3, "time": -1}),
        "(m/s)/s": (Fraction(1), {"length": 1, "time": -2}),
        "g.L(-1)": (Fraction(1000), {"length": -3, "mass": 1}),
    }
    factor, _dims = table[text]
    return factor


EXPECTED_DIMENSIONS = {
    "m2": "length^2",
    "km2": "length^2",
    "km3": "length^3",
    "cm3": "length^3",
    "m2/s2": "length^2.time^-2",
    "(m/s)2": "length^2.time^-2",
    "mg/L": "length^-3.mass^1",
    "kg/m3": "length^-3.mass^1",
    "mm/s": "length^1.time^-1",
    "km/s": "length^1.time^-1",
    "m(-2)": "length^-2",
    "km(-2)": "length^-2",
    "L/s": "length^3.time^-1",
    "(m/s)/s": "length^1.time^-2",
    "g.L(-1)": "length^-3.mass^1",
}


class TestIndependentEvaluator(unittest.TestCase):
    def test_product_factors(self):
        engine = engine_with_vendor()
        for text, expected in ((t, tiny_eval(t)) for t in EXPECTED_DIMENSIONS):
            with self.subTest(text=text):
                unit = engine.resolve_unit(text, "delta", "source")
                self.assertEqual(unit.M.fraction(), expected)
                self.assertEqual(unit.dimension, EXPECTED_DIMENSIONS[text])

    def test_reciprocal_property(self):
        # For a multiplicative unit with independent factor f, converting x
        # from unit U to a unit with factor 1 gives f*x, and converting that
        # back with the inverse factor gives x exactly.
        engine = engine_with_vendor()
        targets = {
            "m2": "m2",
            "m2/s2": "m2/s2",
            "mg/L": "g/m3",
            "kg/m3": "g/m3",
            "L/s": "L/s",
        }
        for text, target in targets.items():
            with self.subTest(text=text):
                unit = engine.resolve_unit(text, "ratio", "source")
                f = unit.M.fraction() / engine.resolve_unit(
                    target, "ratio", "target").M.fraction()
                for value in ["1", "7", "0.25", "-3/4" if False else "-0.75"]:
                    c = engine.convert(value, text, "ratio", target)
                    self.assertEqual(c.standard.fraction(),
                                     engine.parse_value(value).fraction() * f)
                    back = engine.reverse(c.standard, text, "ratio", target)
                    self.assertEqual(back, engine.parse_value(value))

    def test_ratio_is_multiplicatively_scalable(self):
        engine = engine_with_vendor()
        # 3 * (1 mg/L) in g/m3 must equal 3 * (1 mg/L in g/m3).
        one = engine.convert("1", "mg/L", "ratio", "g/m3").standard.fraction()
        three = engine.convert("3", "mg/L", "ratio", "g/m3").standard.fraction()
        self.assertEqual(three, 3 * one)
        half = engine.convert("0.5", "mg/L", "ratio", "g/m3").standard.fraction()
        self.assertEqual(half, one / 2)

    def test_affine_composition_property(self):
        # With exact affine maps M,C from the engine's resolution step,
        # converting A -> K -> B must equal converting A -> B directly.
        # Decimal inputs only (the pipeline deliberately does not accept
        # fractional text as a measurement value).
        engine = engine_with_vendor()
        for a, b in [("Cel", "K"), ("[degF]", "K"), ("Cel", "[degF]"),
                     ("K", "Cel"), ("[degF]", "Cel")]:
            ua = engine.resolve_unit(a, "absolute", "source")
            ub = engine.resolve_unit(b, "absolute", "target")
            for value in ["0", "32", "-40", "100", "300.5"]:
                with self.subTest(a=a, b=b, value=value):
                    x = engine.parse_value(value)
                    basic = ua.M * x + ua.C
                    if basic < 0:
                        # physical range: the direct formula is right but the
                        # pipeline must refuse; assert exactly that.
                        with self.assertRaises(Exception):
                            engine.convert(value, a, "absolute", b)
                        continue
                    direct = (basic - ub.C) / ub.M
                    c = engine.convert(value, a, "absolute", b)
                    self.assertEqual(c.standard, direct)

    def test_delta_is_additive(self):
        engine = engine_with_vendor()
        # delta conversions are linear: f(x+y) = f(x)+f(y)
        fx = engine.convert("2.5", "[degF]/s", "delta", "K/s").standard.fraction()
        fy = engine.convert("7.5", "[degF]/s", "delta", "K/s").standard.fraction()
        fxy = engine.convert("10", "[degF]/s", "delta", "K/s").standard.fraction()
        self.assertEqual(fx + fy, fxy)

    def test_absolute_difference_matches_delta(self):
        # difference of two absolute readings equals the delta conversion
        engine = engine_with_vendor()
        for a, b in [("Cel", "Cel"), ("[degF]", "[degF]"), ("Cel", "[degF]")]:
            with self.subTest(a=a, b=b):
                t1 = engine.convert("10", a, "absolute", "K").standard.fraction()
                t2 = engine.convert("40", a, "absolute", "K").standard.fraction()
                d_abs = t2 - t1
                d_delta = engine.convert("30", a, "delta", "K").standard.fraction()
                self.assertEqual(d_abs, d_delta)

    def test_negative_temperature_boundary_is_exact(self):
        engine = engine_with_vendor()
        self.assertEqual(
            engine.convert("-273.15", "Cel", "absolute", "K").standard.fraction(),
            F("0"))
        self.assertEqual(
            engine.convert("-459.67", "[degF]", "absolute", "K").standard.fraction(),
            F("0"))
        # one exact step below the boundary is invalid
        for value, unit in [("-273.1501", "Cel"), ("-459.6701", "[degF]")]:
            with self.subTest(value=value):
                with self.assertRaises(Exception):
                    engine.convert(value, unit, "absolute", "K")


if __name__ == "__main__":
    unittest.main()
