"""Conversion semantics per quantity kind, with hand-computed expectations.

The expected values below are written as literal ``Fraction`` arithmetic done
by hand, not by the product's parser.
"""

from __future__ import annotations

import unittest

from support import F, engine_with_vendor

from unitpipe.engine import (CATEGORY_DIMENSION, CATEGORY_KIND,
                             CATEGORY_RANGE, CATEGORY_SYNTAX,
                             ConversionProblem)


def convert(engine, value, unit, kind, target):
    return engine.convert(value, unit, kind, target)


class TestSpecExamples(unittest.TestCase):
    def test_spec_examples(self):
        engine = engine_with_vendor()
        cases = [
            ("0", "Cel", "absolute", "K", F("27315/100")),          # 5463/20
            ("0", "Cel", "delta", "K", F("0")),
            ("10", "Cel", "delta", "K", F("10")),
            ("32", "[degF]", "absolute", "K", F("27315/100")),      # 5463/20
            ("1", "mg/L", "ratio", "g/m3", F("1")),
            ("18", "[degF]/s", "delta", "K/s", F("10")),
        ]
        for value, unit, kind, target, expected in cases:
            with self.subTest(value=value, unit=unit, kind=kind):
                c = convert(engine, value, unit, kind, target)
                self.assertEqual(c.standard.fraction(), expected)
                self.assertEqual(str(c.standard.str()), str(expected.numerator)
                                 if expected.denominator == 1
                                 else f"{expected.numerator}/{expected.denominator}")

    def test_cel_absolute_hand_computation(self):
        # 0 Cel -> 0 + 273.15 = 273.15 = 5463/20 K
        c = convert(engine_with_vendor(), "0", "Cel", "absolute", "K")
        self.assertEqual(c.standard.fraction(), F(5463, 20))
        self.assertEqual(c.standard.str(), "5463/20")

    def test_degf_absolute_hand_computation(self):
        # 32 degF -> (32 + 459.67) * 5/9 = 491.67 * 5/9 = 273.15 = 5463/20
        c = convert(engine_with_vendor(), "32", "[degF]", "absolute", "K")
        self.assertEqual(c.standard.fraction(), F(49167, 100) * F(5, 9))
        self.assertEqual(c.standard.fraction(), F(5463, 20))

    def test_mg_per_l_hand_computation(self):
        # 1 mg/L = 1e-3 g / 1e-3 m^3 = 1 g/m^3
        c = convert(engine_with_vendor(), "1", "mg/L", "ratio", "g/m3")
        self.assertEqual(c.standard.fraction(), F("1"))

    def test_degf_per_s_delta_hand_computation(self):
        # 18 degF/s delta = 18 * 5/9 K/s = 10 K/s (no offset)
        c = convert(engine_with_vendor(), "18", "[degF]/s", "delta", "K/s")
        self.assertEqual(c.standard.fraction(), F("10"))

    def test_cel_per_s_as_absolute_is_rejected(self):
        with self.assertRaises(ConversionProblem) as ctx:
            convert(engine_with_vendor(), "1", "Cel/s", "absolute", "K/s")
        self.assertEqual(ctx.exception.category, CATEGORY_KIND)

    def test_absolute_compound_even_if_dimension_cancels(self):
        for unit in ["Cel.m/m", "Cel/m.m", "Cel2/Cel", "(Cel).s/s"]:
            with self.subTest(unit=unit):
                with self.assertRaises(ConversionProblem) as ctx:
                    convert(engine_with_vendor(), "1", unit, "absolute", "K")
                self.assertEqual(ctx.exception.category, CATEGORY_KIND)

    def test_absolute_power_rejected(self):
        with self.assertRaises(ConversionProblem) as ctx:
            convert(engine_with_vendor(), "1", "Cel2", "absolute", "K2")
        self.assertEqual(ctx.exception.category, CATEGORY_KIND)


class TestDelta(unittest.TestCase):
    def test_delta_ignores_offsets(self):
        engine = engine_with_vendor()
        c = convert(engine, "100", "[degF]", "delta", "K")
        self.assertEqual(c.standard.fraction(), F(100 * 5, 9))
        self.assertEqual(c.basic_value.fraction(), F(500, 9))

    def test_delta_cel_and_degf_compounds(self):
        engine = engine_with_vendor()
        self.assertEqual(
            convert(engine, "1", "Cel/s", "delta", "K/s").standard.fraction(),
            F("1"))
        self.assertEqual(
            convert(engine, "9", "[degF]/s", "delta", "K/s").standard.fraction(),
            F("5"))
        self.assertEqual(
            convert(engine, "5", "mK/s", "delta", "K/s").standard.fraction(),
            F("1/200"))

    def test_delta_to_offset_target_has_no_offset(self):
        engine = engine_with_vendor()
        # 10 K delta -> Cel delta is still 10, not 10 - 273.15
        c = convert(engine, "10", "K", "delta", "Cel")
        self.assertEqual(c.standard.fraction(), F("10"))
        self.assertEqual(c.target.C.fraction(), F("0"))


class TestRatio(unittest.TestCase):
    def test_ratio_rejects_offset_units_even_at_zero(self):
        engine = engine_with_vendor()
        for value in ["0", "1", "-5"]:
            with self.subTest(value=value):
                with self.assertRaises(ConversionProblem) as ctx:
                    convert(engine, value, "Cel", "ratio", "K")
                self.assertEqual(ctx.exception.category, CATEGORY_KIND)

    def test_ratio_multiplicative_units(self):
        engine = engine_with_vendor()
        c = convert(engine, "2.5", "mg/L", "ratio", "g/m3")
        self.assertEqual(c.standard.fraction(), F(5, 2))
        c = convert(engine, "1", "L", "ratio", "m3")
        self.assertEqual(c.standard.fraction(), F(1, 1000))
        c = convert(engine, "1", "m3", "ratio", "L")
        self.assertEqual(c.standard.fraction(), F(1000))


class TestDimensions(unittest.TestCase):
    def test_dimension_mismatch_is_isolated(self):
        engine = engine_with_vendor()
        with self.assertRaises(ConversionProblem) as ctx:
            convert(engine, "1", "m", "delta", "s")
        self.assertEqual(ctx.exception.category, CATEGORY_DIMENSION)

    def test_offset_unit_cannot_cross_dimensions(self):
        engine = engine_with_vendor()
        with self.assertRaises(ConversionProblem) as ctx:
            convert(engine, "1", "Cel", "absolute", "m")
        self.assertEqual(ctx.exception.category, CATEGORY_DIMENSION)

    def test_volume_is_length_cubed(self):
        engine = engine_with_vendor()
        c = convert(engine, "1", "L", "ratio", "m3")
        self.assertEqual(c.source.dimension, "length^3")
        self.assertEqual(c.standard.fraction(), F(1, 1000))


class TestPhysicalRange(unittest.TestCase):
    def test_negative_absolute_temperature(self):
        engine = engine_with_vendor()
        with self.assertRaises(ConversionProblem) as ctx:
            convert(engine, "-300", "Cel", "absolute", "K")
        self.assertEqual(ctx.exception.category, CATEGORY_RANGE)
        self.assertEqual(ctx.exception.evidence["kelvin"], "-537/20")

    def test_just_below_absolute_zero_fahrenheit(self):
        engine = engine_with_vendor()
        with self.assertRaises(ConversionProblem) as ctx:
            convert(engine, "-459.68", "[degF]", "absolute", "K")
        self.assertEqual(ctx.exception.category, CATEGORY_RANGE)

    def test_exactly_absolute_zero_is_allowed(self):
        engine = engine_with_vendor()
        c = convert(engine, "-459.67", "[degF]", "absolute", "K")
        self.assertEqual(c.standard.fraction(), F("0"))
        c = convert(engine, "-273.15", "Cel", "absolute", "K")
        self.assertEqual(c.standard.fraction(), F("0"))

    def test_negative_delta_is_fine(self):
        engine = engine_with_vendor()
        c = convert(engine, "-500", "Cel", "delta", "K")
        self.assertEqual(c.standard.fraction(), F("-500"))


class TestExactDecimals(unittest.TestCase):
    def test_decimal_strings_are_exact(self):
        engine = engine_with_vendor()
        cases = [
            ("0.1", "m", "m", F(1, 10)),
            ("-0.0001", "m", "m", F(-1, 10000)),
            ("+3.50", "m", "m", F(7, 2)),
            (".5", "m", "m", F(1, 2)),
            ("5.", "m", "m", F(5)),
            ("00012.3400", "m", "m", F(1234, 100)),
            ("0.000000000000000001", "m", "m", F(1, 10 ** 18)),
        ]
        for value, unit, target, expected in cases:
            with self.subTest(value=value):
                c = convert(engine, value, unit, "delta", target)
                self.assertEqual(c.standard.fraction(), expected)

    def test_invalid_decimals_are_syntax_problems(self):
        engine = engine_with_vendor()
        for value in ["", " ", "1e3", "nan", "inf", "1,5", "0x10", "1 2",
                      "--1", "1.2.3", "abc", None, 3, 1.5]:
            with self.subTest(value=value):
                with self.assertRaises(ConversionProblem) as ctx:
                    convert(engine, value, "m", "delta", "m")
                self.assertEqual(ctx.exception.category, CATEGORY_SYNTAX)

    def test_float_input_never_reaches_the_math(self):
        engine = engine_with_vendor()
        with self.assertRaises(ConversionProblem):
            convert(engine, 0.1, "m", "delta", "m")


class TestReverseRoundTrip(unittest.TestCase):
    def test_round_trip_exact(self):
        engine = engine_with_vendor()
        values = ["0", "10", "32", "-40", "0.1", "123.456", "-273.15",
                  "0.333", "179.999999"]
        for unit, kind, target in [
                ("Cel", "absolute", "K"), ("[degF]", "absolute", "K"),
                ("K", "absolute", "[degF]"), ("Cel", "absolute", "[degF]"),
                ("K", "delta", "Cel"), ("[degF]/s", "delta", "K/s"),
                ("mg/L", "ratio", "g/m3"), ("L", "ratio", "m3"),
                ("m2/s2", "ratio", "m2/s2")]:
            for value in values:
                with self.subTest(unit=unit, kind=kind, value=value):
                    try:
                        c = convert(engine, value, unit, kind, target)
                    except ConversionProblem as exc:
                        # only absolute temperature range errors may occur
                        self.assertEqual(exc.category, CATEGORY_RANGE)
                        continue
                    back = engine.reverse(c.standard, unit, kind, target)
                    original = engine.parse_value(value)
                    self.assertEqual(back, original)
                    check = engine.reverse_check(
                        value, unit, kind, target, c.standard.str())
                    self.assertTrue(check["matches"])

    def test_reverse_is_not_string_equality(self):
        engine = engine_with_vendor()
        c = convert(engine, "0.50", "Cel", "absolute", "K")
        check = engine.reverse_check("0.5", "Cel", "absolute", "K",
                                     c.standard.str())
        self.assertTrue(check["matches"])
        self.assertEqual(check["recovered"]["fraction"], "1/2")

    def test_reverse_mismatch_detected(self):
        engine = engine_with_vendor()
        c = convert(engine, "1", "Cel", "absolute", "K")
        check = engine.reverse_check("2", "Cel", "absolute", "K",
                                     c.standard.str())
        self.assertFalse(check["matches"])


if __name__ == "__main__":
    unittest.main()
