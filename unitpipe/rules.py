"""Rule-set loading: field bindings, vendor dictionaries, batch orchestration."""

from __future__ import annotations

import json
import sqlite3
from typing import Dict, List, Optional, Tuple

from . import pipeline, storage
from .defs import StructuralError
from .engine import (ConversionEngine, FieldRule, check_field_rules,
                     load_field_rules)
from .vendor import VendorDictionary


class RulePackage:
    """Everything a conversion needs, loaded and structurally verified."""

    def __init__(self, *, rules_path: str, vendor_path: Optional[str],
                 fields: Dict[str, FieldRule], engine: ConversionEngine,
                 vendor: VendorDictionary, field_checks: List[dict]):
        self.rules_path = rules_path
        self.vendor_path = vendor_path
        self.fields = fields
        self.engine = engine
        self.vendor = vendor
        self.field_checks = field_checks


def load_field_rules_file(path: str) -> Dict[str, FieldRule]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except OSError as exc:
        raise StructuralError(f"{path}: cannot read rules file: {exc}") from None
    except json.JSONDecodeError as exc:
        raise StructuralError(f"{path}: rules file is not valid JSON: {exc}") from None
    return load_field_rules(obj, path)


def load_vendor_file(path: Optional[str]) -> VendorDictionary:
    vendor = VendorDictionary()
    if path is None:
        return vendor
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        raise StructuralError(f"{path}: cannot read vendor file: {exc}") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StructuralError(f"{path}: vendor file is not valid UTF-8: {exc}") from None
    if "\x00" in text:
        raise StructuralError(f"{path}: vendor file contains a NUL byte")
    lines = [(i + 1, ln) for i, ln in enumerate(text.split("\n"))]
    vendor.load_jsonl(lines, path)
    return vendor


def build_package(rules_path: str, vendor_path: Optional[str] = None
                  ) -> RulePackage:
    fields = load_field_rules_file(rules_path)
    vendor = load_vendor_file(vendor_path)
    engine = ConversionEngine(vendor)
    # All vendor relations are read and verified before anything converts.
    engine.finalize_vendor()
    engine.analyzer.verify_all()
    # Rule targets are checked after verification so a conflicted component
    # is reported with its proper category instead of as a syntax problem.
    # Target units are builtins in practice; a vendor target that belongs to
    # a conflicted component would make the rule itself unusable.
    checks = check_field_rules(fields, engine)
    return RulePackage(rules_path=rules_path, vendor_path=vendor_path,
                       fields=fields, engine=engine, vendor=vendor,
                       field_checks=checks)


def analyze_vendor(package: RulePackage) -> List[dict]:
    out = []
    for cid, nodes in package.engine.analyzer.components():
        entry = {"component_id": cid,
                 "nodes": [f"{n[0]}:{n[1]}" for n in nodes],
                 "dimension": package.vendor.nodes[nodes[0]].dimension,
                 "kinds": {}}
        for kind in ("absolute", "delta", "ratio"):
            state = package.engine.analyzer.state(cid, kind)
            entry["kinds"][kind] = {
                "state": state.state,
                "edges_checked": state.edges_checked,
                "witnesses": state.witnesses,
            }
        out.append(entry)
    return out


def rule_checks(package: RulePackage) -> List[dict]:
    checks: List[dict] = []
    checks.append({
        "name": "grammar_published",
        "status": "ok",
        "detail": {"grammar": "docs/grammar.md", "builtin_symbols": [
            "m", "s", "g", "L", "K", "Cel", "[degF]"],
            "prefixes": {"k": 3, "c": -2, "m": -3, "u": -6}},
    })
    for check in package.field_checks:
        checks.append({"name": f"field_rule:{check['field']}",
                       "status": "ok", "detail": check})
    comps = analyze_vendor(package)
    checks.append({
        "name": "vendor_components",
        "status": "ok",
        "detail": comps,
    })
    for entry in comps:
        for kind, info in entry["kinds"].items():
            state = info["state"]
            checks.append({
                "name": f"vendor_consistency:{entry['component_id']}:{kind}",
                "status": "ok" if state == "ok" else state,
                "detail": {"component_id": entry["component_id"],
                           "nodes": entry["nodes"], "dimension": entry["dimension"],
                           "quantity_kind": kind, "state": state},
            })
    return checks


def run_batch(conn: sqlite3.Connection, package: RulePackage, *,
              input_path: str, good_path: Optional[str] = None,
              isolated_path: Optional[str] = None,
              run_id: Optional[str] = None,
              chunk_size: int = 65536) -> pipeline.BatchResult:
    result = pipeline.run_batch(
        conn, input_path=input_path, rules_path=package.rules_path,
        vendor_path=package.vendor_path, engine=package.engine,
        fields=package.fields, good_path=good_path,
        isolated_path=isolated_path, run_id=run_id, chunk_size=chunk_size)
    store = storage.Store(conn)
    for check in package.field_checks:
        store.add_field_rule(result.run_id, check)
    store.add_vendor_dictionary(result.run_id, package.vendor)
    analyzer = package.engine.analyzer
    for cid, _nodes in analyzer.components():
        for kind in ("absolute", "delta", "ratio"):
            store.add_vendor_analysis(result.run_id,
                                      analyzer.state(cid, kind))
    for check in rule_checks(package):
        store.add_rule_check(result.run_id, check["name"], check["status"],
                             check["detail"])
    conn.commit()
    return result
