"""Command-line interface.

Commands
--------
``rules``     publish the grammar, unit table, field rules, and vendor status
``normalize`` stream a measurement file into SQLite plus good/isolated JSONL
``explain``   per-line explanation (byte offset, parse tree, rules, arithmetic)
``reverse``   invert a normalized value using the stored original unit and kind
``vendor``    print the per-kind vendor component analysis
``show``      inspect stored records, isolations, provenance, or runs
``init``      initialise an empty SQLite store
"""

from __future__ import annotations

import argparse
import json
import os
from typing import List, Optional

from . import rules as rules_mod
from . import storage
from .defs import StructuralError
from .engine import ConversionProblem, load_field_rules
from .rational import q


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False))


def _open_store(path: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return storage.connect(path)


def _load_package(args):
    try:
        return rules_mod.build_package(args.rules, args.vendor)
    except StructuralError as exc:
        print(json.dumps({"status": "incomplete", "structural_error": str(exc)},
                         indent=2))
        return None


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    conn = _open_store(args.db)
    conn.close()
    _print({"status": "ok", "db": os.path.abspath(args.db)})
    return 0


def cmd_rules(args) -> int:
    package = _load_package(args)
    if package is None:
        return 3
    checks = rules_mod.rule_checks(package)
    out = {
        "grammar": _grammar_text(),
        "fields": [c for c in checks if c["name"].startswith("field_rule:")],
        "vendor": analyze_for_output(package),
        "checks": checks,
    }
    _print(out)
    return 0


def _grammar_text() -> dict:
    return {
        "grammar": [
            "expr     := factor ((\"/\" | \".\") factor)*   # left-associative",
            "factor   := primary exponent?",
            "primary  := atom | \"(\" expr \")\"",
            "exponent := DIGITS | \"(\" [\"+\"|\"-\"] DIGITS [\")\"]",
            "atom     := SYMBOL | \"[degF]\" | \"[\" namespace \":\" code \"]\"",
            "SYMBOL   := longest legal builtin symbol with optional prefix",
        ],
        "builtin_units": {
            "m": {"dimension": "length", "scale": "1", "offset": "0"},
            "s": {"dimension": "time", "scale": "1", "offset": "0"},
            "g": {"dimension": "mass", "scale": "1", "offset": "0"},
            "K": {"dimension": "temperature", "scale": "1", "offset": "0"},
            "Cel": {"dimension": "temperature", "scale": "1",
                    "offset": "5463/20", "affine": True,
                    "definition": "x_K = x_Cel + 273.15"},
            "[degF]": {"dimension": "temperature", "scale": "5/9",
                       "offset": "45967/180", "affine": True,
                       "definition": "x_K = (x_degF + 459.67) * 5/9"},
            "L": {"dimension": "length^3", "scale": "1/1000", "offset": "0",
                  "definition": "L = 10^-3 m^3"},
        },
        "prefixes": {"k": "10^3", "c": "10^-2", "m": "10^-3", "u": "10^-6"},
        "quantity_kinds": {
            "absolute": "y_basic = M*x + C; single atom only",
            "delta": "y_basic = M*x; offsets never apply",
            "ratio": "y_basic = M*x; offset units rejected",
        },
        "lexical_policy": "longest legal unit symbol wins: m is metre, "
                          "mg is milligram, mm2 is square millimetre",
        "total_max_exponent": 9999,
    }


def analyze_for_output(package) -> List[dict]:
    return rules_mod.analyze_vendor(package)


def cmd_normalize(args) -> int:
    package = _load_package(args)
    if package is None:
        return 3
    conn = _open_store(args.db)
    result = rules_mod.run_batch(
        conn, package, input_path=args.input, good_path=args.good,
        isolated_path=args.isolated, run_id=args.run_id)
    conn.close()
    _print(result.json())
    return 0 if result.status == "complete" else 4


def _latest_run_id(conn) -> Optional[str]:
    row = storage.Store(conn).latest_run()
    return row["run_id"] if row else None


def cmd_explain(args) -> int:
    conn = _open_store(args.db)
    store = storage.Store(conn)
    run_id = args.run or _latest_run_id(conn)
    if not run_id:
        _print({"error": "no runs in store"})
        return 2
    row = store.record(run_id, args.line)
    if row is not None:
        explanation = json.loads(row["explanation_json"])
        explanation["stored_fields"] = {
            "field": row["field"], "value": row["value_text"],
            "unit": row["unit_text"], "quantity_kind": row["quantity_kind"],
            "target_unit": row["target_unit"],
            "standard_fraction": row["standard_fraction"],
            "basic": {"num": row["basic_num"], "den": row["basic_den"]},
            "dimensions": {"source": row["source_dimension"],
                           "target": row["target_dimension"]},
            "source_transform": {"M": row["source_multiplier"],
                                 "C": row["source_offset"]},
            "target_transform": {"M": row["target_multiplier"],
                                 "C": row["target_offset"]},
        }
        _print(explanation)
        return 0
    iso = store.isolated_row(run_id, args.line)
    if iso is not None:
        prov = store.provenance(run_id, args.line)
        _print({
            "run_id": run_id,
            "byte_line_no": args.line,
            "status": "isolated",
            "category": iso["category"],
            "reason": iso["reason"],
            "message": iso["message"],
            "evidence": json.loads(iso["evidence_json"]),
            "provenance": prov,
        })
        return 1
    _print({"error": f"line {args.line} not present in run {run_id}"})
    return 2


def cmd_reverse(args) -> int:
    package = _load_package(args)
    if package is None:
        return 3
    conn = _open_store(args.db)
    store = storage.Store(conn)
    run_id = args.run
    line_no = args.line
    if line_no is not None:
        if run_id is None:
            run_id = _latest_run_id(conn)
        row = store.record(run_id, line_no) if run_id else None
        if row is None:
            _print({"error": f"no normalized record at {run_id}:{line_no}"})
            return 1
        unit_text = row["unit_text"]
        kind = row["quantity_kind"]
        target = row["target_unit"]
        standard = row["standard_fraction"]
        original_value = row["value_text"]
        original_is_stored = True
    else:
        if args.unit is None or args.kind is None or args.target is None:
            _print({"error": "provide --line or --unit/--kind/--target"})
            return 2
        unit_text, kind, target = args.unit, args.kind, args.target
        standard = args.standard
        original_value = args.value
        original_is_stored = False
    if standard is None:
        _print({"error": "a standard value is required (--standard or --line)"})
        return 2

    try:
        check = package.engine.reverse_check(
            original_value, unit_text, kind, target, standard)
    except ConversionProblem as exc:
        check = {"matches": False, "category": exc.category,
                 "reason": exc.reason, "message": exc.message}
    if not original_is_stored:
        check["note"] = ("comparison uses the --value you supplied" if
                         args.value is not None else
                         "no original value supplied; only the recovered "
                         "rational is reported")
    result = {
        "unit": unit_text,
        "quantity_kind": kind,
        "target_unit": target,
        "standard_fraction": standard,
        **check,
    }
    check_id = store.add_reverse_check(
        run_id=run_id, byte_line_no=line_no, unit_text=unit_text,
        quantity_kind=kind, target_unit=target, standard_fraction=standard,
        result=result)
    conn.commit()
    result["check_id"] = check_id
    _print(result)
    return 0 if check.get("matches") else 1


def cmd_vendor(args) -> int:
    package = _load_package(args)
    if package is None:
        return 3
    states = {}
    for cid, nodes in package.engine.analyzer.components():
        for kind in ("absolute", "delta", "ratio"):
            state = package.engine.analyzer.state(cid, kind)
            states[f"{cid}:{kind}"] = state.json()
    _print({"components": analyze_for_output(package),
            "relations": [r.lookup_hint() for r in package.vendor.relations],
            "states": states})
    return 0


def cmd_show(args) -> int:
    conn = _open_store(args.db)
    store = storage.Store(conn)
    if args.what == "runs":
        rows = conn.execute("SELECT * FROM runs ORDER BY created_at, rowid").fetchall()
        _print([dict(r) for r in rows])
    elif args.what == "records":
        run_id = args.run or _latest_run_id(conn)
        rows = conn.execute(
            "SELECT byte_line_no, field, value_text, unit_text, quantity_kind, "
            "target_unit, standard_fraction, raw_sha256 FROM records "
            "WHERE run_id=? ORDER BY byte_line_no", (run_id,)).fetchall()
        _print({"run_id": run_id, "records": [dict(r) for r in rows]})
    elif args.what == "isolated":
        run_id = args.run or _latest_run_id(conn)
        rows = conn.execute(
            "SELECT byte_line_no, category, reason, message FROM isolated "
            "WHERE run_id=? ORDER BY byte_line_no", (run_id,)).fetchall()
        _print({"run_id": run_id, "isolated": [dict(r) for r in rows]})
    elif args.what == "provenance":
        if args.line is None:
            _print({"error": "--line required"})
            return 2
        run_id = args.run or _latest_run_id(conn)
        _print(store.provenance(run_id, args.line))
    else:
        _print({"error": f"unknown show target {args.what}"})
        return 2
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="unitpipe",
        description="Exact unit normalisation for experiment result data")
    p.add_argument("--db", default="unitpipe.sqlite3", help="SQLite store path")
    sub = p.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="create the SQLite store")
    init.set_defaults(func=cmd_init)

    def common(sp):
        sp.add_argument("--rules", required=True, help="field rule JSON file")
        sp.add_argument("--vendor", default=None,
                        help="vendor relation JSONL file (optional)")

    rules_p = sub.add_parser("rules", help="publish grammar, unit table, checks")
    common(rules_p)
    rules_p.set_defaults(func=cmd_rules)

    norm = sub.add_parser("normalize", help="stream-normalize a measurement file")
    common(norm)
    norm.add_argument("--input", required=True)
    norm.add_argument("--good", default=None, help="accepted JSONL output")
    norm.add_argument("--isolated", default=None, help="isolated JSONL output")
    norm.add_argument("--run-id", default=None,
                      help="explicit run id (default: random)")
    norm.set_defaults(func=cmd_normalize)

    expl = sub.add_parser("explain", help="explain one stored line")
    expl.add_argument("--line", type=int, required=True)
    expl.add_argument("--run", default=None)
    expl.set_defaults(func=cmd_explain)

    rev = sub.add_parser("reverse", help="reverse-check a normalized value")
    common(rev)
    rev.add_argument("--line", type=int, default=None,
                     help="stored line to reverse using its saved unit/kind")
    rev.add_argument("--run", default=None)
    rev.add_argument("--unit", default=None)
    rev.add_argument("--kind", default=None)
    rev.add_argument("--target", default=None)
    rev.add_argument("--standard", default=None,
                     help="standard value as a canonical fraction")
    rev.add_argument("--value", default=None,
                     help="original decimal value to compare against")
    rev.set_defaults(func=cmd_reverse)

    vend = sub.add_parser("vendor", help="show vendor component analysis")
    common(vend)
    vend.set_defaults(func=cmd_vendor)

    show = sub.add_parser("show", help="inspect stored state")
    show.add_argument("what", choices=["runs", "records", "isolated", "provenance"])
    show.add_argument("--run", default=None)
    show.add_argument("--line", type=int, default=None)
    show.set_defaults(func=cmd_show)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except StructuralError as exc:
        _print({"status": "incomplete", "structural_error": str(exc)})
        return 3
    except RuntimeError as exc:
        _print({"status": "error", "message": str(exc)})
        return 2
