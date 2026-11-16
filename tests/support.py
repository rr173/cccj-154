"""Test helpers.  Expected values are hand-computed, never produced by the
product's parser: every expected factor is written down as a literal
``Fraction`` here.
"""

from __future__ import annotations

import json
import os
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from unitpipe import storage  # noqa: E402
from unitpipe.engine import ConversionEngine  # noqa: E402
from unitpipe.rules import build_package  # noqa: E402
from unitpipe.vendor import VendorDictionary  # noqa: E402


def F(num, den=None) -> Fraction:
    """Hand-written exact expectation; never derived from the product code."""
    if den is None:
        return Fraction(num)
    return Fraction(num, den)


def engine_with_vendor(lines=None) -> ConversionEngine:
    vendor = VendorDictionary()
    if lines:
        vendor.load_jsonl([(i + 1, l) for i, l in enumerate(lines)])
    engine = ConversionEngine(vendor)
    engine.finalize_vendor()
    return engine


def build_vendor(lines):
    vendor = VendorDictionary()
    vendor.load_jsonl([(i + 1, l) for i, l in enumerate(lines)])
    engine = ConversionEngine(vendor)
    engine.finalize_vendor()
    return vendor, engine


def write_rules(tmp_path, fields: dict) -> str:
    path = tmp_path / "fields.json"
    path.write_text(json.dumps({"fields": fields}), encoding="utf-8")
    return str(path)


def write_vendor(tmp_path, lines) -> str:
    path = tmp_path / "vendor.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def write_lines(tmp_path, name: str, lines, newline: bytes = b"\n") -> str:
    path = tmp_path / name
    data = newline.join(
        (l.encode("utf-8") if isinstance(l, str) else l) for l in lines)
    path.write_bytes(data + newline)
    return str(path)


def open_db(tmp_path, name: str = "store.sqlite3"):
    return storage.connect(str(tmp_path / name))


DEFAULT_FIELDS = {
    "T_abs": {"quantity_kind": "absolute", "target_unit": "K",
              "dimension": "temperature"},
    "T_delta": {"quantity_kind": "delta", "target_unit": "K",
                "dimension": "temperature"},
    "T_rate": {"quantity_kind": "delta", "target_unit": "K/s",
               "dimension": "temperature"},
    "T_cel": {"quantity_kind": "absolute", "target_unit": "Cel",
              "dimension": "temperature"},
    "T_f": {"quantity_kind": "absolute", "target_unit": "[degF]",
            "dimension": "temperature"},
    "C_ratio": {"quantity_kind": "ratio", "target_unit": "g/m3",
                "dimension": "mass"},
    "L_ratio": {"quantity_kind": "ratio", "target_unit": "m",
                "dimension": "length"},
    "L_abs": {"quantity_kind": "absolute", "target_unit": "m",
              "dimension": "length"},
    "M_ratio": {"quantity_kind": "ratio", "target_unit": "g",
                "dimension": "mass"},
    "V_ratio": {"quantity_kind": "ratio", "target_unit": "m3",
                "dimension": "length"},
}


def standard_package(tmp_path, vendor_lines=None):
    rules = write_rules(tmp_path, DEFAULT_FIELDS)
    vendor_path = write_vendor(tmp_path, vendor_lines) if vendor_lines else None
    return build_package(rules, vendor_path)
