"""Lexer, grammar, and exact-factor tests.

Every expected factor is a literal written by hand below; none is produced by
the product's own parser.
"""

from __future__ import annotations

import unittest

from support import F, engine_with_vendor

from unitpipe.defs import UnknownCodeError, UnitSyntaxError
from unitpipe.units import parse_unit_text


def resolve(engine, text):
    return engine.resolve_unit(text, "delta", "source")


class TestLexicalPolicy(unittest.TestCase):
    def test_atom_and_prefix_factors(self):
        cases = [
            # text, dimension, exact factor (hand-computed)
            ("m", "length^1", F("1")),
            ("km", "length^1", F("1000")),
            ("cm", "length^1", F("1/100")),
            ("mm", "length^1", F("1/1000")),
            ("um", "length^1", F("1/1000000")),
            ("mg", "mass^1", F("1/1000")),        # milli + gram
            ("kg", "mass^1", F("1000")),
            ("ug", "mass^1", F("1/1000000")),
            ("cg", "mass^1", F("1/100")),
            ("g", "mass^1", F("1")),
            ("s", "time^1", F("1")),
            ("ks", "time^1", F("1000")),
            ("K", "temperature^1", F("1")),
            ("mK", "temperature^1", F("1/1000")),
            ("L", "length^3", F("1/1000")),       # L = 1e-3 m^3
            ("mL", "length^3", F("1/1000000")),   # 1e-3 * 1e-3
            ("uL", "length^3", F("1/1000000000")),
        ]
        engine = engine_with_vendor()
        for text, dim, factor in cases:
            with self.subTest(text=text):
                unit = resolve(engine, text)
                self.assertEqual(unit.dimension, dim)
                self.assertEqual(unit.M.fraction(), factor)

    def test_m_is_metre_not_milli(self):
        unit = resolve(engine_with_vendor(), "m")
        self.assertEqual(unit.dimension, "length^1")
        self.assertEqual(unit.M.fraction(), F("1"))
        self.assertIsNone(unit.atoms[0].prefix)

    def test_mg_is_milligram(self):
        unit = resolve(engine_with_vendor(), "mg")
        self.assertEqual(unit.dimension, "mass^1")
        self.assertEqual(unit.M.fraction(), F("1/1000"))
        self.assertEqual(unit.atoms[0].prefix, "m")

    def test_unknown_codes_are_never_guessed(self):
        engine = engine_with_vendor()
        for bad in ["degF", "furlong", "dam", "mCel", "kCel", "cel", "M", "G",
                    "mm1", "m1", "m0", "in", "ft", "[inch]", "[foo]", "h"]:
            with self.subTest(bad=bad):
                with self.assertRaises(Exception):
                    parse_unit_text(bad)


class TestCompounds(unittest.TestCase):
    def test_compound_factors(self):
        cases = [
            ("m/s", "length^1.time^-1", F("1")),
            ("m.s", "length^1.time^1", F("1")),
            ("m/s/s", "length^1.time^-2", F("1")),
            ("m2/s2", "length^2.time^-2", F("1")),
            ("(m/s)2", "length^2.time^-2", F("1")),
            ("m(-2)", "length^-2", F("1")),
            ("m(-2).s", "length^-2.time^1", F("1")),
            ("(m.s)/g", "length^1.mass^-1.time^1", F("1")),
            ("g/m3", "length^-3.mass^1", F("1")),
            ("mg/L", "length^-3.mass^1", F("1")),        # 1e-3 g / 1e-3 m3
            ("g.L(-1).m(-3)", "length^-6.mass^1", F("1000")),  # L^-1 = 1000 m^-3
            ("mm2", "length^2", F("1/1000000")),          # (1e-3)^2
            ("km3", "length^3", F("1000000000")),         # (1e3)^3
            ("(mm)2", "length^2", F("1/1000000")),
            ("(km/s)2", "length^2.time^-2", F("1000000")),
            ("mm3/s", "length^3.time^-1", F("1/1000000000")),
            ("(m/s)(-1)", "length^-1.time^1", F("1")),
            ("(km)(2)", "length^2", F("1000000")),
            ("s(-1)", "time^-1", F("1")),
            ("(kg/m3)", "length^-3.mass^1", F("1000")),
            ("ug/mL", "length^-3.mass^1", F("1")),        # 1e-6 g / 1e-6 m3
            ("mg/dL", None, None),
        ]
        engine = engine_with_vendor()
        for text, dim, factor in cases:
            if dim is None:
                # "dL" is not a defined prefix+symbol (d is not in the subset)
                with self.subTest(text=text):
                    with self.assertRaises(Exception):
                        resolve(engine, text)
                continue
            with self.subTest(text=text):
                unit = resolve(engine, text)
                self.assertEqual(unit.dimension, dim)
                self.assertEqual(unit.M.fraction(), factor)

    def test_dimension_cancellation(self):
        unit = resolve(engine_with_vendor(), "mg.L(-1).g(-1).m3")
        self.assertEqual(unit.dimension, "1")
        # (1e-3 g) * (1 / 1e-3 m3) * (1/g) * m3 = 1
        self.assertEqual(unit.M.fraction(), F("1"))

    def test_left_associativity(self):
        a = resolve(engine_with_vendor(), "m/s/kg")
        b = resolve(engine_with_vendor(), "m/s.kg")
        self.assertEqual(a.dimension, "length^1.mass^-1.time^-1")
        self.assertEqual(b.dimension, "length^1.mass^1.time^-1")

    def test_whitespace(self):
        unit = resolve(engine_with_vendor(), " mg / L ")
        self.assertEqual(unit.dimension, "length^-3.mass^1")
        self.assertEqual(unit.M.fraction(), F("1"))

    def test_parenthesised_offset_unit_alone(self):
        engine = engine_with_vendor()
        unit = engine.resolve_unit("((Cel))", "absolute", "source")
        self.assertTrue(unit.affine)
        self.assertEqual(unit.M.fraction(), F("1"))


class TestSyntaxErrors(unittest.TestCase):
    def test_invalid_syntax(self):
        cases = ["", "   ", "m^2", "m**2", "m.", ".m", "/m", "m/", "m//s",
                 "()", "m()", "(m", "m)", "[degF", "m(-)", "m(2", "m(2.5)",
                 "m(0)", "m(1)", "m(-0)", "m\u0000s", "m2.5",
                 "m(+)", "m(  )", "m-2", "1", "m1", "m00", "m01"]
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(UnitSyntaxError):
                    parse_unit_text(text)

    def test_offset_unit_in_compound_is_rejected_in_absolute(self):
        engine = engine_with_vendor()
        for text in ["Cel/s", "[degF]/s", "Cel.K", "Cel2", "(Cel)/s",
                     "Cel.m/m"]:
            with self.subTest(text=text):
                with self.assertRaises(Exception):
                    engine.resolve_unit(text, "absolute", "source")

    def test_vendor_namespace_syntax_in_measurement_units(self):
        # The atom parses; it is only usable once the vendor declares it.
        parsed = parse_unit_text("[acme:X]")
        self.assertEqual(parsed.atoms[0].kind, "vendor")
        engine = engine_with_vendor()
        with self.assertRaises(Exception):
            engine.resolve_unit("[acme:X]", "delta", "source")


class TestOffsetUnits(unittest.TestCase):
    def test_cel_absolute_affine(self):
        unit = engine_with_vendor().resolve_unit("Cel", "absolute", "source")
        self.assertTrue(unit.affine)
        self.assertEqual(unit.M.fraction(), F("1"))
        self.assertEqual(unit.C.fraction(), F("27315/100"))   # 273.15

    def test_degf_absolute_affine(self):
        unit = engine_with_vendor().resolve_unit("[degF]", "absolute", "source")
        self.assertTrue(unit.affine)
        self.assertEqual(unit.M.fraction(), F("5/9"))
        self.assertEqual(unit.C.fraction(), F("45967/180"))   # 459.67*5/9

    def test_delta_ignores_offsets(self):
        unit = engine_with_vendor().resolve_unit("Cel", "delta", "source")
        self.assertFalse(unit.affine)
        self.assertEqual(unit.M.fraction(), F("1"))
        self.assertEqual(unit.C.fraction(), F("0"))

    def test_delta_compound_offset_units_use_multipliers(self):
        engine = engine_with_vendor()
        cel = engine.resolve_unit("Cel/s", "delta", "source")
        self.assertEqual(cel.dimension, "time^-1.temperature^1")
        self.assertEqual(cel.M.fraction(), F("1"))
        f = engine.resolve_unit("[degF]/s", "delta", "source")
        self.assertEqual(f.M.fraction(), F("5/9"))

    def test_prefixed_offset_unit_rejected(self):
        with self.assertRaises(Exception):
            engine_with_vendor().resolve_unit("mCel", "absolute", "source")


if __name__ == "__main__":
    unittest.main()
