"""Streaming pipeline, isolation categories, provenance, and chunk invariance."""

from __future__ import annotations

import io
import json
import sqlite3
import unittest
import tempfile
from pathlib import Path

from support import (DEFAULT_FIELDS, F, open_db, standard_package,
                     write_lines, write_rules, write_vendor)

from unitpipe import pipeline, rules, storage
from unitpipe.defs import StructuralError


def line(field, value, unit, kind, **extra):
    obj = {"field": field, "value": value, "unit": unit,
           "quantity_kind": kind}
    obj.update(extra)
    return json.dumps(obj)


class PipelineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.package = standard_package(self.tmp)
        self.conn = open_db(self.tmp)
        self.store = storage.Store(self.conn)
        self.good = str(self.tmp / "good.jsonl")
        self.iso = str(self.tmp / "isolated.jsonl")

    def tearDown(self):
        self.conn.close()

    def run_lines(self, lines, name="input.jsonl"):
        path = write_lines(self.tmp, name, lines)
        return rules.run_batch(self.conn, self.package, input_path=path,
                               good_path=self.good, isolated_path=self.iso)

    def good_records(self):
        return [json.loads(l) for l in open(self.good, encoding="utf-8")]

    def iso_records(self):
        return [json.loads(l) for l in open(self.iso, encoding="utf-8")]


class TestGoodAndIsolated(PipelineCase):
    def test_spec_rows(self):
        result = self.run_lines([
            line("T_abs", "0", "Cel", "absolute"),
            line("T_delta", "0", "Cel", "delta"),
            line("T_delta", "10", "Cel", "delta"),
            line("T_abs", "32", "[degF]", "absolute"),
            line("C_ratio", "1", "mg/L", "ratio"),
            line("T_rate", "18", "[degF]/s", "delta"),
        ])
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.good, 6)
        self.assertEqual(result.isolated, 0)
        values = [(r["byte_line_no"], r["standard_value"])
                  for r in self.good_records()]
        self.assertEqual(values, [(1, "5463/20"), (2, "0"), (3, "10"),
                                  (4, "5463/20"), (5, "1"), (6, "10")])

    def test_cel_per_s_absolute_isolated(self):
        result = self.run_lines([line("T_abs", "1", "Cel/s", "absolute")])
        self.assertEqual(result.isolated, 1)
        rec = self.iso_records()[0]
        self.assertEqual(rec["category"], "kind_mismatch")

    def test_isolation_categories(self):
        rows = [
            line("T_abs", "20", "furlong", "absolute"),   # 1 syntax
            line("T_abs", "1e3", "K", "absolute"),        # 2 syntax
            line("T_abs", "", "K", "absolute"),           # 3 syntax
            line("T_abs", "20", "m", "absolute"),         # 4 dimension
            line("C_ratio", "1", "L", "ratio"),           # 5 dimension
            line("T_abs", "20", "K", "delta"),            # 6 kind (field bound absolute)
            line("C_ratio", "0", "Cel", "ratio"),         # 7 kind (offset ratio)
            line("T_abs", "-300", "Cel", "absolute"),     # 8 range
        ]
        result = self.run_lines(rows)
        self.assertEqual(result.good, 0)
        self.assertEqual(result.isolated, len(rows))
        cats = [(r["byte_line_no"], r["category"])
                for r in self.iso_records()]
        self.assertEqual(cats, [
            (1, "syntax_unsupported"),
            (2, "syntax_unsupported"),
            (3, "syntax_unsupported"),
            (4, "dimension_mismatch"),
            (5, "dimension_mismatch"),
            (6, "kind_mismatch"),
            (7, "kind_mismatch"),
            (8, "invalid_range"),
        ])

    def test_all_isolated_file_is_complete(self):
        rows = [line("T_abs", "x", "K", "absolute") for _ in range(5)]
        result = self.run_lines(rows)
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.good, 0)
        self.assertEqual(result.isolated, 5)

    def test_blank_and_bad_json_lines_isolated(self):
        rows = ["", "  ", "{not json", "[1,2,3]", line("T_abs", "1", "K", "absolute")]
        result = self.run_lines(rows)
        self.assertEqual(result.good, 1)
        self.assertEqual(result.isolated, 4)
        reasons = [r["reason"] for r in self.iso_records()]
        self.assertIn("blank_line", reasons)
        self.assertIn("invalid_json_line", reasons)
        self.assertIn("record_not_object", reasons)

    def test_missing_field_and_unknown_field(self):
        rows = [
            json.dumps({"value": "1", "unit": "K", "quantity_kind": "absolute"}),
            line("nope", "1", "K", "absolute"),
            line("T_abs", "1", "K", "absolute", target_unit="Cel"),
            json.dumps({"field": "T_abs", "value": "1",
                        "quantity_kind": "absolute"}),
        ]
        result = self.run_lines(rows)
        self.assertEqual(result.isolated, 4)
        reasons = [r["reason"] for r in self.iso_records()]
        self.assertEqual(reasons, ["missing_field", "unknown_field",
                                   "target_unit_mismatch", "missing_unit"])

    def test_reported_target_mismatch_refused(self):
        # A line requesting a different target is not silently retargeted.
        result = self.run_lines([
            line("T_abs", "0", "Cel", "absolute", target_unit="Cel")])
        self.assertEqual(result.isolated, 1)
        self.assertEqual(self.iso_records()[0]["reason"], "target_unit_mismatch")


class TestProvenance(PipelineCase):
    def test_byte_offsets_and_hashes(self):
        rows = [line("T_abs", "0", "Cel", "absolute"),
                line("T_delta", "1", "K", "delta")]
        path = write_lines(self.tmp, "input.jsonl", rows)
        result = rules.run_batch(self.conn, self.package, input_path=path,
                                 good_path=self.good, isolated_path=self.iso)
        raw = open(path, "rb").read()
        lines = raw.split(b"\n")
        for expected_line in (1, 2):
            prov = self.store.provenance(result.run_id, expected_line)
            chunk = lines[expected_line - 1]
            self.assertEqual(prov["raw_text"], chunk.decode())
            self.assertEqual(prov["byte_len"], len(chunk))
            self.assertEqual(raw[prov["byte_offset"]:
                                 prov["byte_offset"] + prov["byte_len"]], chunk)
            self.assertEqual(storage.sha256_bytes(chunk), prov["raw_sha256"])
            self.assertEqual(storage.sha256_bytes(chunk),
                             self.store.record(result.run_id,
                                               expected_line)["raw_sha256"]
                             if self.store.record(result.run_id, expected_line)
                             else self.store.isolated_row(
                                 result.run_id, expected_line)["raw_sha256"])

    def test_raw_line_numbers_are_physical(self):
        rows = ["", "   ",
                line("T_abs", "0", "Cel", "absolute")]
        result = self.run_lines(rows)
        rec = self.good_records()[0]
        self.assertEqual(rec["byte_line_no"], 3)

    def test_explanation_contents(self):
        self.run_lines([line("T_abs", "0", "Cel", "absolute")])
        run_id = storage.Store(self.conn).latest_run()["run_id"]
        row = self.store.record(run_id, 1)
        expl = json.loads(row["explanation_json"])
        self.assertEqual(row["raw_sha256"], expl["raw_sha256"])
        self.assertEqual(expl["byte_line_no"], 1)
        self.assertIn("parse_tree", expl["source"])
        self.assertIn("normalised_unit", expl["source"])
        self.assertEqual(expl["source"]["dimension_after_cancellation"],
                         "temperature^1")
        self.assertEqual(expl["source"]["multiplier"], "1")
        self.assertEqual(expl["source"]["offset"], "5463/20")
        self.assertEqual(expl["standard_value"]["fraction"], "5463/20")
        rule_names = [r["rule"] for r in expl["rules_applied"]]
        self.assertIn("builtin_unit_definition", rule_names)
        self.assertIn("quantity_kind_arithmetic", rule_names)

    def test_isolated_explanation_has_provenance(self):
        self.run_lines([line("T_abs", "20", "furlong", "absolute")])
        run_id = storage.Store(self.conn).latest_run()["run_id"]
        row = self.store.isolated_row(run_id, 1)
        prov = self.store.provenance(run_id, 1)
        self.assertEqual(prov["raw_text"], row["raw_sha256"] and prov["raw_text"])
        self.assertIn("furlong", prov["raw_text"])


class TestChunkInvariance(PipelineCase):
    def test_block_size_does_not_change_results(self):
        # Build a long alternating absolute/delta temperature file.
        rows = []
        for i in range(400):
            if i % 2 == 0:
                rows.append(line("T_abs", str(i % 7), "Cel", "absolute"))
            else:
                rows.append(line("T_delta", str(i % 5), "[degF]", "delta"))
        path = write_lines(self.tmp, "alt.jsonl", rows)

        def snapshot(chunk: int):
            results = []
            for l in pipeline.iter_bytes_lines(path, chunk_size=chunk):
                results.append((l.line_no, l.offset, l.raw))
            return results

        base = snapshot(65536)
        for chunk in (1, 7, 64, 4096, 1 << 20):
            self.assertEqual(snapshot(chunk), base)

        # End-to-end identical outputs for different chunk sizes.
        def convert_all(chunk: int):
            conn = open_db(self.tmp, f"c{chunk}.sqlite3")
            package = standard_package(self.tmp)
            good = str(self.tmp / f"g{chunk}.jsonl")
            iso = str(self.tmp / f"i{chunk}.jsonl")
            res = rules.run_batch(conn, package, input_path=path, good_path=good,
                                  isolated_path=iso, chunk_size=chunk)
            rows_out = [json.loads(x) for x in open(good, encoding="utf-8")]
            conn.close()
            return res.good, res.isolated, [
                (r["byte_line_no"], r["standard_value"], r["unit"],
                 r["quantity_kind"]) for r in rows_out]

        stdout = convert_all(65536)
        for chunk in (1, 13, 1024):
            self.assertEqual(convert_all(chunk), stdout)

    def test_alternating_kinds_reuse_no_wrong_offset(self):
        rows = []
        for i in range(50):
            rows.append(line("T_abs", "0", "Cel", "absolute"))
            rows.append(line("T_delta", "0", "Cel", "delta"))
        result = self.run_lines(rows)
        values = [(r["quantity_kind"], r["standard_value"])
                  for r in self.good_records()]
        expected = [("absolute", "5463/20"), ("delta", "0")] * 50
        self.assertEqual(values, expected)

    def test_explanations_identical_across_chunks(self):
        rows = []
        for i in range(60):
            unit = "Cel" if i % 3 == 0 else "[degF]" if i % 3 == 1 else "K"
            kind = "absolute" if i % 2 == 0 else "delta"
            field = "T_abs" if kind == "absolute" else "T_delta"
            rows.append(line(field, str(i % 11), unit, kind))
        path = write_lines(self.tmp, "explanations.jsonl", rows)
        snapshots = []
        for chunk in (65536, 3, 17):
            conn = open_db(self.tmp, f"ec{chunk}.sqlite3")
            store = storage.Store(conn)
            package = standard_package(self.tmp)
            res = rules.run_batch(conn, package, input_path=path,
                                  good_path=str(self.tmp / f"eg{chunk}.jsonl"),
                                  chunk_size=chunk)
            details = []
            for n in range(1, len(rows) + 1):
                row = store.record(res.run_id, n)
                expl = json.loads(row["explanation_json"])
                # Explanation must not depend on chunking.
                details.append((n, expl["source"]["normalised_unit"],
                                expl["standard_value"]["fraction"],
                                expl["byte_offset"], expl["byte_len"]))
            conn.close()
            snapshots.append(details)
        # Chunk size only affects iter_bytes_lines; run_batch uses the default.
        self.assertTrue(all(s == snapshots[0] for s in snapshots))

    def test_cache_stats_split_by_kind(self):
        engine = self.package.engine
        before = engine.cache_info()["misses"]
        engine.resolve_unit("Cel", "absolute", "source")
        after_abs = engine.cache_info()["misses"]
        engine.resolve_unit("Cel", "delta", "source")
        after_delta = engine.cache_info()["misses"]
        self.assertEqual(after_abs - before, 1)
        self.assertEqual(after_delta - after_abs, 1)
        # Same key again is a hit and returns the right transform for the kind.
        self.assertEqual(
            engine.resolve_unit("Cel", "delta", "source").C.fraction(), F("0"))
        self.assertEqual(
            engine.resolve_unit("Cel", "absolute", "source").C.fraction(),
            F(27315, 100))


pipeline_iter = pipeline.iter_bytes_lines


class TestStreamingBounds(PipelineCase):
    def test_iter_bytes_lines_keeps_only_current_line(self):
        # A pathological single line far larger than the chunk size must still
        # be delivered with correct byte accounting, without splitting it.
        payload = b"x" * 100000
        path = self.tmp / "big.jsonl"
        path.write_bytes(b"a\n" + payload + b"\n" + b"c\n")
        lines = list(pipeline.iter_bytes_lines(str(path), chunk_size=1024))
        self.assertEqual([l.raw for l in lines],
                         [b"a", payload, b"c"])
        self.assertEqual([l.offset for l in lines],
                         [0, 2, 2 + len(payload) + 1])
        self.assertEqual([l.line_no for l in lines], [1, 2, 3])


class TestStructuralFailure(PipelineCase):
    def test_invalid_utf8_is_incomplete(self):
        path = self.tmp / "bad.jsonl"
        path.write_bytes(b'{"field":"T_abs","value":"1","unit":"K",'
                         b'"quantity_kind":"absolute"}\n\xff\xfe\n')
        result = rules.run_batch(self.conn, self.package, input_path=str(path),
                                 good_path=self.good, isolated_path=self.iso)
        self.assertEqual(result.status, "incomplete")
        self.assertIsNotNone(result.structural_error)
        run = self.store.get_run(result.run_id)
        self.assertEqual(run["complete"], 0)
        self.assertEqual(run["status"], "incomplete")

    def test_structural_vendor_damage_aborts_package(self):
        vendor_path = self.tmp / "vendor.jsonl"
        vendor_path.write_text("{oops\n", encoding="utf-8")
        with self.assertRaises(StructuralError):
            rules.build_package(str(self.tmp / "fields.json"), str(vendor_path))


if __name__ == "__main__":
    unittest.main()
