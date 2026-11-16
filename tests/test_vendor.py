"""Vendor relation graph: per-kind closure, conflicts, unanchored components.

All relation fixtures are written by hand; expected anchors are derived by
hand from the declared relations, not from the product's analyzer.
"""

from __future__ import annotations

import json
import unittest

from support import F, build_vendor

from unitpipe.defs import StructuralError
from unitpipe.engine import (CATEGORY_CONFLICT, CATEGORY_DIMENSION,
                             CATEGORY_KIND, CATEGORY_UNANCHORED,
                             ConversionProblem)
from unitpipe.vendor import VendorDictionary


def node(source, ns, code, dim):
    return json.dumps({"type": "node", "source": source, "namespace": ns,
                       "code": code, "dimension": dim})


def rel(source, rid, u, v, a, b, dim=None):
    def obj(text):
        ns, _, code = text.partition(":")
        return {"namespace": ns, "code": code}

    def rat(text):
        num, den = (text.split("/") + ["1"])[:2] if "/" in text else (text, "1")
        return {"num": int(num), "den": int(den)}

    body = {"type": "relation", "source": source, "relation_id": rid,
            "u": obj(u), "v": obj(v), "a": rat(a), "b": rat(b)}
    if dim is not None:
        body["dimension"] = dim
    return json.dumps(body)


# Hand-written main exercise: X -> Cel is 2x+10, X -> K is 2x+283.15.
# Because Cel -> K is builtin (+273.15), both paths agree:
#   x_K = 2x_X + 283.15  and  x_K = (2x_X + 10) + 273.15 = 2x_X + 283.15.
MAIN = [
    node("acme", "acme", "X", "temperature"),
    node("acme", "acme", "Y", "length"),
    rel("acme", "X-Cel", "acme:X", "builtin:Cel", "2", "10"),
    rel("acme", "X-K", "acme:X", "builtin:K", "2", "5663/20"),
    rel("acme", "Y-m", "acme:Y", "builtin:m", "2", "0"),
]

# Only X -> K's offset changed to 284.15: absolute must conflict.
CONFLICT = [
    node("acme", "acme", "X", "temperature"),
    node("acme", "acme", "Y", "length"),
    rel("acme", "X-Cel", "acme:X", "builtin:Cel", "2", "10"),
    rel("acme", "X-K", "acme:X", "builtin:K", "2", "5683/20"),
    rel("acme", "Y-m", "acme:Y", "builtin:m", "2", "0"),
]


class TestVendorMainExercise(unittest.TestCase):
    def setUp(self):
        self.vendor, self.engine = build_vendor(MAIN)

    def test_x_anchor_hand_computation(self):
        # From X -> Cel: x_K = 1*(2x + 10) + 273.15 = 2x + 283.15.
        state = self.engine.analyzer.state_for_node(("acme", "X"), "absolute")
        self.assertEqual(state.state, "ok")
        self.assertEqual(state.M[("acme", "X")].fraction(), F("2"))
        self.assertEqual(state.C[("acme", "X")].fraction(), F(28315, 100))

    def test_x_absolute_five(self):
        c = self.engine.convert("5", "[acme:X]", "absolute", "K")
        # 2*5 + 283.15 = 293.15 = 5863/20
        self.assertEqual(c.standard.fraction(), F(5863, 20))

    def test_x_delta_five(self):
        c = self.engine.convert("5", "[acme:X]", "delta", "K")
        self.assertEqual(c.standard.fraction(), F("10"))

    def test_unrelated_length_component_still_works(self):
        c = self.engine.convert("1", "[acme:Y]", "absolute", "m")
        self.assertEqual(c.standard.fraction(), F("2"))
        c = self.engine.convert("1", "[acme:Y]", "delta", "m")
        self.assertEqual(c.standard.fraction(), F("2"))

    def test_builtin_units_still_work(self):
        c = self.engine.convert("0", "Cel", "absolute", "K")
        self.assertEqual(c.standard.fraction(), F(5463, 20))

    def test_per_kind_states_cached_separately(self):
        a = self.engine.analyzer.state_for_node(("acme", "X"), "absolute")
        d = self.engine.analyzer.state_for_node(("acme", "X"), "delta")
        self.assertEqual(a.state, "ok")
        self.assertEqual(d.state, "ok")
        self.assertIsNot(a, d)


class TestVendorConflict(unittest.TestCase):
    def setUp(self):
        self.vendor, self.engine = build_vendor(CONFLICT)

    def test_absolute_component_conflicts(self):
        with self.assertRaises(ConversionProblem) as ctx:
            self.engine.convert("5", "[acme:X]", "absolute", "K")
        exc = ctx.exception
        self.assertEqual(exc.category, CATEGORY_CONFLICT)
        witnesses = exc.evidence["witnesses"]
        self.assertTrue(witnesses)
        w = witnesses[0]
        self.assertFalse(w["consistent"])
        self.assertEqual(w["kind"], "absolute")
        # The closed walk must compose to a=1 but a different offset.
        self.assertEqual(F(w["composed"]["a"]), F("1"))
        self.assertNotEqual(F(w["composed"]["b"]), F("0"))
        # Every step cites a declared relation with its own a,b.
        for step in w["steps"]:
            rel_hint = step["relation"]
            self.assertIn("source", rel_hint)
            self.assertIn("relation_id", rel_hint)
            self.assertIn("a", rel_hint)
            self.assertIn("b", rel_hint)

    def test_delta_survives_absolute_conflict(self):
        c = self.engine.convert("5", "[acme:X]", "delta", "K")
        self.assertEqual(c.standard.fraction(), F("10"))
        state = self.engine.analyzer.state_for_node(("acme", "X"), "delta")
        self.assertEqual(state.state, "ok")

    def test_builtin_temperature_component_conflicts_too(self):
        # The conflict is in the shared temperature component, so a plain
        # absolute Cel conversion through that component must also be refused.
        with self.assertRaises(ConversionProblem) as ctx:
            self.engine.convert("0", "Cel", "absolute", "K")
        self.assertEqual(ctx.exception.category, CATEGORY_CONFLICT)

    def test_length_component_unaffected(self):
        c = self.engine.convert("3", "[acme:Y]", "absolute", "m")
        self.assertEqual(c.standard.fraction(), F("6"))

    def test_witness_can_be_recomputed_from_declared_edges(self):
        state = self.engine.analyzer.state_for_node(("acme", "X"), "absolute")
        w = state.witnesses[0]
        # Recompute the composition independently from the stored step data.
        A, B = F("1"), F("0")
        for step in w["steps"]:
            a = F(step["applied"]["a"])
            b = F(step["applied"]["b"])
            A, B = a * A, a * B + b
        self.assertEqual(A, F(w["composed"]["a"]))
        self.assertEqual(B, F(w["composed"]["b"]))
        self.assertFalse(A == 1 and B == 0)


class TestEdgeOrderAndDirection(unittest.TestCase):
    def _run(self, lines):
        vendor, engine = build_vendor(lines)
        results = {}
        for kind in ("absolute", "delta"):
            try:
                c = engine.convert("5", "[acme:X]", kind, "K")
                results[kind] = ("ok", c.standard.str())
            except ConversionProblem as exc:
                results[kind] = (exc.category, None)
        try:
            c = engine.convert("1", "[acme:Y]", "absolute", "m")
            results["length"] = ("ok", c.standard.str())
        except ConversionProblem as exc:
            results["length"] = (exc.category, None)
        return results

    def test_reordered_edges_same_result(self):
        reordered = [MAIN[2], MAIN[4], MAIN[0], MAIN[3], MAIN[1]]
        self.assertEqual(self._run(MAIN), self._run(reordered))

    def test_reversed_relations_with_exact_inverse(self):
        # Reverse X -> Cel as Cel -> X with a=1/2, b=-5.
        reversed_lines = [
            node("acme", "acme", "X", "temperature"),
            node("acme", "acme", "Y", "length"),
            rel("acme", "Cel-X", "builtin:Cel", "acme:X", "1/2", "-5"),
            rel("acme", "X-K", "acme:X", "builtin:K", "2", "5663/20"),
            rel("acme", "Y-m", "acme:Y", "builtin:m", "2", "0"),
        ]
        self.assertEqual(self._run(MAIN), self._run(reversed_lines))

    def test_redundant_consistent_edge(self):
        # Exact duplicates under new ids, plus an equivalent route via Z.
        redundant = MAIN + [
            node("acme", "acme", "Z", "temperature"),
            rel("acme", "Z-X", "acme:Z", "acme:X", "3", "1"),
            rel("acme", "Z-Cel", "acme:Z", "builtin:Cel", "6", "12"),
            rel("acme", "X-Cel-2", "acme:X", "builtin:Cel", "2", "10"),
        ]
        result = self._run(redundant)
        self.assertEqual(result["absolute"], ("ok", "5863/20"))
        self.assertEqual(result["delta"], ("ok", "10"))
        self.assertEqual(result["length"], ("ok", "2"))

    def test_redundant_edge_as_new_path(self):
        # A second, consistent relation via a new node Z.
        extra = MAIN + [
            node("acme", "acme", "Z", "temperature"),
            rel("acme", "Z-X", "acme:Z", "acme:X", "3", "1"),
            rel("acme", "Z-K", "acme:Z", "builtin:K", "6", "5703/20"),
        ]
        vendor, engine = build_vendor(extra)
        c = engine.convert("1", "[acme:Z]", "absolute", "K")
        # x_K = 6*z + 285.15; z=1 -> 291.15 = 5823/20
        self.assertEqual(c.standard.fraction(), F(5823, 20))

    def test_late_contradiction_revokes_earlier_success(self):
        vendor = VendorDictionary()
        vendor.load_jsonl([(i + 1, l) for i, l in enumerate(MAIN)])
        engine = __import__("unitpipe.engine", fromlist=["ConversionEngine"]
                            ).ConversionEngine(vendor)
        engine.finalize_vendor()
        c = engine.convert("5", "[acme:X]", "absolute", "K")
        self.assertEqual(c.standard.fraction(), F(5863, 20))
        # A late redundant relation contradicts absolute semantics and must
        # revoke the earlier, apparently usable path.
        late = rel("acme", "X-K-2", "acme:X", "builtin:K", "2", "5683/20")
        vendor.load_jsonl([(100, late)])
        engine.vendor_changed()
        engine.finalize_vendor()
        with self.assertRaises(ConversionProblem) as ctx:
            engine.convert("5", "[acme:X]", "absolute", "K")
        self.assertEqual(ctx.exception.category, CATEGORY_CONFLICT)
        # And the freshly calculated delta result still works.
        self.assertEqual(
            engine.convert("5", "[acme:X]", "delta", "K").standard.fraction(),
            F("10"))

    def test_converting_before_finalize_is_refused(self):
        from unitpipe.engine import ConversionEngine
        vendor = VendorDictionary()
        vendor.load_jsonl([(i + 1, l) for i, l in enumerate(MAIN)])
        engine = ConversionEngine(vendor)
        with self.assertRaises(RuntimeError):
            engine.convert("1", "[acme:X]", "delta", "K")


class TestUnanchored(unittest.TestCase):
    def test_unanchored_component(self):
        lines = [
            node("acme", "acme", "Q", "temperature"),
            node("acme", "acme", "R", "temperature"),
            rel("acme", "Q-R", "acme:Q", "acme:R", "2", "5"),
        ]
        vendor, engine = build_vendor(lines)
        state = engine.analyzer.state_for_node(("acme", "Q"), "delta")
        self.assertEqual(state.state, "unanchored")
        with self.assertRaises(ConversionProblem) as ctx:
            engine.convert("1", "[acme:Q]", "delta", "K")
        self.assertEqual(ctx.exception.category, CATEGORY_UNANCHORED)

    def test_unanchored_but_internally_consistent_cycle(self):
        lines = [
            node("acme", "acme", "Q", "length"),
            node("acme", "acme", "R", "length"),
            rel("acme", "Q-R", "acme:Q", "acme:R", "2", "0"),
            rel("acme", "R-Q", "acme:R", "acme:Q", "1/2", "0"),
        ]
        vendor, engine = build_vendor(lines)
        with self.assertRaises(ConversionProblem) as ctx:
            engine.convert("1", "[acme:Q]", "ratio", "m")
        self.assertEqual(ctx.exception.category, CATEGORY_UNANCHORED)


class TestCycles(unittest.TestCase):
    def test_consistent_cycle_is_allowed(self):
        # X -> Y (2x+1), Y -> X (1/2 x - 1/2) closes exactly to identity.
        lines = [
            node("acme", "acme", "X", "length"),
            node("acme", "acme", "Y", "length"),
            rel("acme", "X-Y", "acme:X", "acme:Y", "2", "1"),
            rel("acme", "Y-X", "acme:Y", "acme:X", "1/2", "-1/2"),
            rel("acme", "X-m", "acme:X", "builtin:m", "1", "0"),
        ]
        vendor, engine = build_vendor(lines)
        state = engine.analyzer.state_for_node(("acme", "X"), "absolute")
        self.assertEqual(state.state, "ok")
        # x_X = (3 - 1)/2 = 1; x_m = x_X = 1
        c = engine.convert("3", "[acme:Y]", "absolute", "m")
        self.assertEqual(c.standard.fraction(), F("1"))

    def test_consistent_cycle_delta_offsets_ignored(self):
        # Multipliers close (2 * 1/2 = 1) but offsets do not; delta is fine.
        lines = [
            node("acme", "acme", "X", "length"),
            node("acme", "acme", "Y", "length"),
            rel("acme", "X-Y", "acme:X", "acme:Y", "2", "1"),
            rel("acme", "Y-X", "acme:Y", "acme:X", "1/2", "0"),
            rel("acme", "X-m", "acme:X", "builtin:m", "1", "0"),
        ]
        vendor, engine = build_vendor(lines)
        a = engine.analyzer.state_for_node(("acme", "X"), "absolute")
        d = engine.analyzer.state_for_node(("acme", "X"), "delta")
        self.assertEqual(a.state, "conflict")
        self.assertEqual(d.state, "ok")

    def test_multiplier_conflict_hits_both_kinds(self):
        lines = [
            node("acme", "acme", "X", "length"),
            node("acme", "acme", "Y", "length"),
            rel("acme", "X-Y", "acme:X", "acme:Y", "2", "0"),
            rel("acme", "Y-X", "acme:Y", "acme:X", "1/3", "0"),
            rel("acme", "X-m", "acme:X", "builtin:m", "1", "0"),
        ]
        vendor, engine = build_vendor(lines)
        for kind in ("absolute", "delta", "ratio"):
            with self.subTest(kind=kind):
                state = engine.analyzer.state_for_node(("acme", "X"), kind)
                self.assertEqual(state.state, "conflict")
                with self.assertRaises(ConversionProblem) as ctx:
                    engine.convert("1", "[acme:X]", kind, "m")
                self.assertEqual(ctx.exception.category, CATEGORY_CONFLICT)


class TestOffsetOnlyCycle(unittest.TestCase):
    """A cycle of pure offset shifts: absolute conflicts, delta survives.

    x_Z = x_X + 1 and x_X = x_Z + 1 compose to x_X = x_X + 2 on the closed
    walk: multiplier 1, offset 2.  Every vendor edge individually anchors
    correctly against the builtin relation, so only the closure check can
    catch this.
    """

    LINES = [
        node("acme", "acme", "X", "temperature"),
        node("acme", "acme", "Z", "temperature"),
        rel("acme", "X-Z", "acme:X", "acme:Z", "1", "1"),
        rel("acme", "Z-X", "acme:Z", "acme:X", "1", "1"),
        rel("acme", "X-K", "acme:X", "builtin:K", "1", "5463/20"),
    ]

    def test_absolute_conflict_with_offset_witness(self):
        vendor, engine = build_vendor(self.LINES)
        with self.assertRaises(ConversionProblem) as ctx:
            engine.convert("0", "[acme:X]", "absolute", "K")
        self.assertEqual(ctx.exception.category, CATEGORY_CONFLICT)
        witnesses = ctx.exception.evidence["witnesses"]
        self.assertTrue(witnesses)
        for w in witnesses:
            self.assertFalse(w["consistent"])
            self.assertEqual(F(w["composed"]["a"]), F("1"))
            self.assertNotEqual(F(w["composed"]["b"]), F("0"))

    def test_delta_is_unaffected(self):
        vendor, engine = build_vendor(self.LINES)
        c = engine.convert("5", "[acme:X]", "delta", "K")
        self.assertEqual(c.standard.fraction(), F("5"))
        c = engine.convert("5", "[acme:Z]", "delta", "K")
        self.assertEqual(c.standard.fraction(), F("5"))

    def test_builtin_temperature_refused_for_absolute_only(self):
        vendor, engine = build_vendor(self.LINES)
        with self.assertRaises(ConversionProblem) as ctx:
            engine.convert("0", "Cel", "absolute", "K")
        self.assertEqual(ctx.exception.category, CATEGORY_CONFLICT)
        self.assertEqual(
            engine.convert("0", "Cel", "delta", "K").standard.fraction(), F("0"))


class TestBuiltinAnchorCheck(unittest.TestCase):
    """Cross-check: tree anchors must agree with the published builtin table.

    The builtin ``Cel -> K`` and ``degF -> K`` relations are also graph edges,
    so in normal operation the per-edge closure check already catches an
    inconsistent anchor.  This test removes those two builtin edges from a
    dictionary and re-runs the analyzer, which forces the independent anchor
    comparison; the same contradiction must still be reported, and the
    witness must cite the published definition.
    """

    LINES = [
        node("acme", "acme", "X", "temperature"),
        node("acme", "acme", "Z", "temperature"),
        rel("acme", "X-Z", "acme:X", "acme:Z", "1", "1"),
        rel("acme", "Z-Cel", "acme:Z", "builtin:Cel", "1", "2"),
        # x_K = x_X + 279.15; with the chain above the tree claims
        # x_K = x_Cel + 276.15, while the builtin table says +273.15.
        rel("acme", "X-K", "acme:X", "builtin:K", "1", "5583/20"),
    ]

    def _analyzer_without_builtin_edges(self):
        from unitpipe.vendor import Analyzer, VendorDictionary
        vendor = VendorDictionary()
        vendor.load_jsonl([(i + 1, l) for i, l in enumerate(self.LINES)])
        vendor.relations = [r for r in vendor.relations if r.origin != "builtin"]
        return vendor, Analyzer(vendor)

    def test_anchor_check_catches_offset_mismatch(self):
        vendor, analyzer = self._analyzer_without_builtin_edges()
        cid = analyzer.component_of()[("acme", "X")]
        state = analyzer.state(cid, "absolute")
        self.assertEqual(state.state, "conflict")
        witness = state.witnesses[0]
        self.assertFalse(witness["consistent"])
        self.assertEqual(F(witness["composed"]["a"]), F("1"))
        self.assertNotEqual(F(witness["composed"]["b"]), F("0"))
        self.assertEqual(witness["conflict_edge"]["origin"], "builtin")
        self.assertIn("builtin/anchor", witness["conflict_edge"]["relation_id"])

    def test_anchor_check_ignores_offsets_for_delta(self):
        vendor, analyzer = self._analyzer_without_builtin_edges()
        cid = analyzer.component_of()[("acme", "X")]
        self.assertEqual(analyzer.state(cid, "delta").state, "ok")

    def test_normal_graph_conflicts_via_edge_closure(self):
        vendor, engine = build_vendor(self.LINES)
        with self.assertRaises(ConversionProblem) as ctx:
            engine.convert("0", "[acme:X]", "absolute", "K")
        self.assertEqual(ctx.exception.category, CATEGORY_CONFLICT)
        self.assertEqual(
            engine.convert("5", "[acme:X]", "delta", "K").standard.fraction(),
            F("5"))


class TestPermutationInvariance(unittest.TestCase):
    CASES = {
        "absolute_X": ("5", "[acme:X]", "absolute", "K"),
        "delta_X": ("5", "[acme:X]", "delta", "K"),
        "absolute_Y": ("1", "[acme:Y]", "absolute", "m"),
        "absolute_Cel": ("0", "Cel", "absolute", "K"),
        "delta_degF": ("9", "[degF]/s", "delta", "K/s"),
    }

    def _snapshot(self, lines):
        vendor, engine = build_vendor(lines)
        snap = {}
        for name, args in self.CASES.items():
            try:
                snap[name] = ("ok", engine.convert(*args).standard.str())
            except ConversionProblem as exc:
                snap[name] = (exc.category, exc.reason)
        for cid, nodes in engine.analyzer.components():
            for kind in ("absolute", "delta", "ratio"):
                snap[f"state:{cid}:{kind}"] = engine.analyzer.state(cid, kind).state
        return snap

    def test_reversed_and_shuffled_edges(self):
        import itertools
        inverses = [
            node("acme", "acme", "X", "temperature"),
            node("acme", "acme", "Y", "length"),
            # Cel -> X is the exact inverse of x_Cel = 2 x_X + 10
            rel("acme", "Cel-X", "builtin:Cel", "acme:X", "1/2", "-5"),
            # K -> X is the exact inverse of x_K = 2 x_X + 283.15
            rel("acme", "K-X", "builtin:K", "acme:X", "1/2", "-5663/40"),
            rel("acme", "m-Y", "builtin:m", "acme:Y", "1/2", "0"),
        ]
        base = self._snapshot(MAIN)
        for perm in itertools.islice(itertools.permutations(inverses), 0, 120, 17):
            with self.subTest(perm=perm[0][:20]):
                self.assertEqual(self._snapshot(list(perm)), base)

    def test_conflict_snapshot_is_stable_under_reordering(self):
        import itertools
        base = self._snapshot(CONFLICT)
        self.assertEqual(base["absolute_X"][0], "unit_conflict")
        for perm in itertools.islice(itertools.permutations(CONFLICT), 0, 120, 19):
            with self.subTest(perm=perm[0][:20]):
                self.assertEqual(self._snapshot(list(perm)), base)


class TestComponentWideConflictIsolation(unittest.TestCase):
    """A measurement using a *locally consistent* edge is still isolated.

    The contradiction is between X -> Cel and X -> K.  A line using only
    X -> Cel and the builtin Cel -> K still touches the same component, and
    the whole component is unusable for absolute, so the line is isolated
    with the actual contradictory cycle as evidence.
    """

    LINES = CONFLICT + [
        node("acme", "acme", "W", "temperature"),
        # W is only related to Cel, the "convenient" edge, with no offset.
        rel("acme", "W-Cel", "acme:W", "builtin:Cel", "1", "0"),
    ]

    def test_local_edge_still_isolated(self):
        vendor, engine = build_vendor(self.LINES)
        for unit in ("[acme:W]", "[acme:X]", "Cel", "[degF]"):
            with self.subTest(unit=unit):
                with self.assertRaises(ConversionProblem) as ctx:
                    engine.convert("0", unit, "absolute", "K")
                self.assertEqual(ctx.exception.category, CATEGORY_CONFLICT)
        # delta uses only multipliers, all 1, and stays usable
        self.assertEqual(
            engine.convert("0", "[acme:W]", "delta", "K").standard.fraction(),
            F("0"))

    def test_no_convenient_path_around_conflict(self):
        vendor, engine = build_vendor(self.LINES)
        state = engine.analyzer.state_for_node(("acme", "W"), "absolute")
        self.assertEqual(state.state, "conflict")
        # Witness edges must all be derivable from declared relations.
        rel_ids = {r.relation_id for r in vendor.relations}
        for w in state.witnesses:
            for step in w["steps"]:
                self.assertIn(step["relation"]["relation_id"], rel_ids)


class TestStructuralErrors(unittest.TestCase):
    def _load(self, lines):
        VendorDictionary().load_jsonl([(i + 1, l) for i, l in enumerate(lines)])

    def test_dimension_conflict_is_structural(self):
        lines = [
            node("acme", "acme", "X", "temperature"),
            node("acme", "acme", "Z", "length"),
            rel("acme", "X-Z", "acme:X", "acme:Z", "1", "0"),
        ]
        with self.assertRaises(StructuralError):
            self._load(lines)

    def test_endpoint_code_collision_across_sources(self):
        lines = [
            node("acme", "acme", "X", "temperature"),
            node("other", "other", "X", "temperature"),
            rel("acme", "same-code", "acme:X", "builtin:K", "1", "0"),
            rel("other", "same-code", "other:X", "builtin:K", "1", "0"),
        ]
        vendor = VendorDictionary()
        vendor.load_jsonl([(i + 1, l) for i, l in enumerate(lines)])
        # Identities are distinct: two separate components, no merge.
        comps = vendor.components()
        # acme:X and other:X both attach to builtin:K but stay distinct
        # identities; the remaining builtin components stand alone.
        self.assertEqual(len(comps), 4)
        keys = {k for _, nodes in comps for k in nodes}
        self.assertIn(("acme", "X"), keys)
        self.assertIn(("other", "X"), keys)

    def test_namespace_cannot_be_redeemed_by_another_source(self):
        lines = [
            node("acme", "shared", "X", "temperature"),
            node("other", "shared", "Y", "temperature"),
        ]
        with self.assertRaises(StructuralError):
            self._load(lines)

    def test_builtin_namespace_reserved(self):
        lines = [node("acme", "builtin", "X", "temperature")]
        with self.assertRaises(StructuralError):
            self._load(lines)

    def test_nonpositive_multiplier(self):
        lines = [
            node("acme", "acme", "X", "temperature"),
            rel("acme", "r", "acme:X", "builtin:K", "0", "0"),
        ]
        with self.assertRaises(StructuralError):
            self._load(lines)

    def test_float_ratios_rejected(self):
        body = json.dumps({"type": "node", "source": "acme", "namespace": "acme",
                           "code": "X", "dimension": "temperature"})
        rel_bad = json.dumps({
            "type": "relation", "source": "acme", "relation_id": "r",
            "u": {"namespace": "acme", "code": "X"},
            "v": {"namespace": "builtin", "code": "K"},
            "a": {"num": 2.0, "den": 1}, "b": {"num": 0, "den": 1}})
        with self.assertRaises(StructuralError):
            self._load([body, rel_bad])

    def test_denominator_must_be_positive(self):
        body = json.dumps({"type": "node", "source": "acme", "namespace": "acme",
                           "code": "X", "dimension": "temperature"})
        bad = json.dumps({
            "type": "relation", "source": "acme", "relation_id": "r",
            "u": {"namespace": "acme", "code": "X"},
            "v": {"namespace": "builtin", "code": "K"},
            "a": {"num": 2, "den": -1}, "b": {"num": 0, "den": 1}})
        with self.assertRaises(StructuralError):
            self._load([body, bad])

    def test_undeclared_endpoint(self):
        with self.assertRaises(StructuralError):
            self._load([node("acme", "acme", "X", "temperature"),
                        rel("acme", "r", "acme:X", "acme:Q", "1", "0")])

    def test_unknown_builtin_endpoint(self):
        with self.assertRaises(StructuralError):
            self._load([node("acme", "acme", "X", "temperature"),
                        rel("acme", "r", "acme:X", "builtin:L", "1", "0")])

    def test_bad_relation_id_duplicate(self):
        lines = [
            node("acme", "acme", "X", "temperature"),
            rel("acme", "dup", "acme:X", "builtin:K", "1", "5463/20"),
            rel("acme", "dup", "acme:X", "builtin:Cel", "1", "0"),
        ]
        with self.assertRaises(StructuralError):
            self._load(lines)

    def test_malformed_json_is_structural(self):
        with self.assertRaises(StructuralError):
            self._load(["{not json"])

    def test_bad_namespace_characters(self):
        with self.assertRaises(StructuralError):
            self._load([node("acme", "acme-1", "X", "temperature")])


class TestRatioWithVendor(unittest.TestCase):
    def test_ratio_rejects_vendor_offset_nodes(self):
        vendor, engine = build_vendor(MAIN)
        # X anchors with offset 283.15, so it is not a ratio unit.
        with self.assertRaises(ConversionProblem) as ctx:
            engine.convert("1", "[acme:X]", "ratio", "K")
        self.assertEqual(ctx.exception.category, CATEGORY_KIND)

    def test_ratio_accepts_offset_free_vendor_component(self):
        lines = [
            node("acme", "acme", "Q", "length"),
            rel("acme", "Q-m", "acme:Q", "builtin:m", "2", "0"),
        ]
        vendor, engine = build_vendor(lines)
        c = engine.convert("3", "[acme:Q]", "ratio", "m")
        self.assertEqual(c.standard.fraction(), F("6"))

    def test_vendor_unit_in_compound(self):
        vendor, engine = build_vendor(MAIN)
        c = engine.convert("5", "[acme:X]/s", "delta", "K/s")
        self.assertEqual(c.standard.fraction(), F("10"))
        # dimension check: X is temperature, so [acme:X]/s has dim T/t
        self.assertEqual(c.source.dimension, "time^-1.temperature^1")


if __name__ == "__main__":
    unittest.main()
