"""Rule checks, CLI integration, and StoredState/provenance round-trips."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from support import (DEFAULT_FIELDS, F, open_db, standard_package,
                     write_lines, write_rules, write_vendor)

from unitpipe import cli, rules as rules_mod, storage
from unitpipe.engine import ConversionProblem


ROOT = Path(__file__).resolve().parents[1]


def run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, json.loads(buf.getvalue())


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rules = write_rules(self.tmp, DEFAULT_FIELDS)
        self.db = str(self.tmp / "store.sqlite3")
        storage.connect(self.db).close()

    def rows(self, lines, name="input.jsonl"):
        return write_lines(self.tmp, name, lines)


def line(field, value, unit, kind, **extra):
    obj = {"field": field, "value": value, "unit": unit,
           "quantity_kind": kind}
    obj.update(extra)
    return json.dumps(obj)


class TestCli(CliCase):
    def test_rules_output_publishes_grammar(self):
        code, out = run_cli(["--db", self.db, "rules", "--rules", self.rules])
        self.assertEqual(code, 0)
        self.assertIn("grammar", out)
        self.assertIn("Cel", out["grammar"]["builtin_units"])
        self.assertEqual(out["grammar"]["builtin_units"]["L"]["scale"], "1/1000")
        names = [c["name"] for c in out["checks"]]
        self.assertIn("grammar_published", names)

    def test_normalize_explain_reverse_show(self):
        path = self.rows([
            line("T_abs", "0", "Cel", "absolute"),
            line("T_abs", "20", "furlong", "absolute"),
        ])
        code, out = run_cli(["--db", self.db, "normalize", "--rules", self.rules,
                             "--input", path, "--good",
                             str(self.tmp / "g.jsonl"), "--isolated",
                             str(self.tmp / "i.jsonl"), "--run-id", "r1"])
        self.assertEqual(code, 0)
        self.assertEqual(out["good"], 1)
        self.assertEqual(out["isolated"], 1)

        code, expl = run_cli(["--db", self.db, "explain", "--line", "1",
                              "--run", "r1"])
        self.assertEqual(code, 0)
        self.assertEqual(expl["standard_value"]["fraction"], "5463/20")
        self.assertEqual(expl["byte_line_no"], 1)

        code, iso = run_cli(["--db", self.db, "explain", "--line", "2",
                             "--run", "r1"])
        self.assertEqual(code, 1)
        self.assertEqual(iso["category"], "syntax_unsupported")

        code, rev = run_cli(["--db", self.db, "reverse", "--rules", self.rules,
                             "--line", "1", "--run", "r1"])
        self.assertEqual(code, 0)
        self.assertTrue(rev["matches"])
        self.assertEqual(rev["recovered"]["fraction"], "0")

        code, shown = run_cli(["--db", self.db, "show", "records",
                               "--run", "r1"])
        self.assertEqual(code, 0)
        self.assertEqual(shown["records"][0]["standard_fraction"], "5463/20")

        code, prov = run_cli(["--db", self.db, "show", "provenance",
                              "--line", "1", "--run", "r1"])
        self.assertEqual(code, 0)
        self.assertEqual(prov["byte_line_no"], 1)
        self.assertTrue(prov["raw_text"].startswith('{"field": "T_abs"'))

    def test_reverse_cli_standalone(self):
        code, rev = run_cli(["--db", self.db, "reverse", "--rules", self.rules,
                             "--unit", "mg/L", "--kind", "ratio",
                             "--target", "g/m3", "--standard", "1",
                             "--value", "1"])
        self.assertEqual(code, 0)
        self.assertTrue(rev["matches"])

    def test_reverse_cli_detects_mismatch(self):
        code, rev = run_cli(["--db", self.db, "reverse", "--rules", self.rules,
                             "--unit", "Cel", "--kind", "absolute",
                             "--target", "K", "--standard", "5463/20",
                             "--value", "1"])
        self.assertEqual(code, 1)
        self.assertFalse(rev["matches"])

    def test_structural_rules_failure_exit_code(self):
        bad = self.tmp / "bad_rules.json"
        bad.write_text("{not json", encoding="utf-8")
        code, out = run_cli(["--db", self.db, "rules", "--rules", str(bad)])
        self.assertEqual(code, 3)
        self.assertEqual(out["status"], "incomplete")

    def test_subprocess_entrypoint(self):
        path = self.rows([line("T_delta", "10", "Cel", "delta")])
        proc = subprocess.run(
            [sys.executable, "-I", str(ROOT / "run.py"), "--db", self.db,
             "normalize", "--rules", self.rules, "--input", path],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["good"], 1)


class TestRuleChecks(CliCase):
    def test_field_checks_include_multipliers(self):
        package = rules_mod.build_package(self.rules)
        checks = {c["field"]: c for c in package.field_checks}
        # mg/L -> g/m3 is exactly 1
        self.assertEqual(checks["C_ratio"]["multiplier"], "1")
        # The target unit m3 has multiplier 1 by definition.
        self.assertEqual(checks["V_ratio"]["multiplier"], "1")
        self.assertEqual(checks["V_ratio"]["resolved_dimension"], "length^3")
        # The factor of L lives on the source side and stays exact.
        engine = package.engine
        self.assertEqual(
            engine.resolve_unit("L", "ratio", "source").M.fraction(), F(1, 1000))

    def test_bad_selectors_are_structural(self):
        for spec in [
            {"quantity_kind": "weird", "target_unit": "K"},
            {"quantity_kind": "absolute", "target_unit": "furlong"},
            {"quantity_kind": "absolute", "target_unit": "Cel", "dimension": "mass"},
            {"quantity_kind": "absolute"},
        ]:
            with self.subTest(spec=spec):
                path = write_rules(self.tmp, {"X": spec})
                with self.assertRaises(Exception):
                    rules_mod.build_package(path)

    def test_kind_and_target_persisted(self):
        path = self.rows([line("T_abs", "0", "Cel", "absolute")])
        result = rules_mod.run_batch(
            open_db(self.tmp, "x.sqlite3"),
            rules_mod.build_package(self.rules), input_path=path)
        conn = open_db(self.tmp, "x.sqlite3")
        row = conn.execute("SELECT * FROM field_rules WHERE field='T_abs'").fetchone()
        self.assertEqual(row["quantity_kind"], "absolute")
        self.assertEqual(row["target_unit"], "K")
        # The target unit K is offset-free; the source Cel offset is exact.
        self.assertEqual(row["offset"], "0")
        conn2 = open_db(self.tmp, "x.sqlite3")
        rec = conn2.execute("SELECT * FROM records WHERE field='T_abs'").fetchone()
        self.assertEqual(rec["source_offset"], "5463/20")


if __name__ == "__main__":
    unittest.main()
