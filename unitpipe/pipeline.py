"""Streaming batch normalisation with per-line isolation.

Input is JSON Lines, one measurement per line::

    {"field": "T_set", "value": "0", "unit": "Cel",
     "quantity_kind": "absolute"}

Optional keys: ``id``, ``source``, ``target_unit`` (must equal the target that
the field rule binds; a different target is a kind/rule mismatch), and
``record`` metadata that is copied into the explanation.  Empty ``value`` and
invalid decimals are isolated, never guessed.  Other keys are ignored but
recorded in the explanation.

The reader works on raw bytes, splits only on ``\\n``, and only keeps the
current line (plus a bounded buffer) in memory, so input may be much larger
than RAM.  Valid and isolated records are written to SQLite and to two JSONL
output files as soon as each line is decided.  A structural file error
(malformed JSON that is not line-local, missing schema, damaged vendor file)
aborts the run with ``status='incomplete'``; line-level problems never do.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from . import storage
from .engine import (CATEGORY_KIND, CATEGORY_SYNTAX, Conversion,
                     ConversionEngine, ConversionProblem)
from .defs import StructuralError
from .rational import Q


class StructuralFileError(Exception):
    """The package cannot be completed: structural damage, not a bad line."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class ByteLine:
    line_no: int          # 1-based physical line number
    offset: int
    raw: bytes            # without trailing newline


def iter_bytes_lines(path: str, chunk_size: int = 65536) -> Iterator[ByteLine]:
    """Yield physical lines as bytes using a bounded buffer."""
    offset = 0
    line_no = 1
    buf = b""
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            buf += chunk
            while True:
                idx = buf.find(b"\n")
                if idx < 0:
                    break
                raw = buf[:idx]
                yield ByteLine(line_no, offset, raw)
                offset += idx + 1
                line_no += 1
                buf = buf[idx + 1:]
        if buf:
            yield ByteLine(line_no, offset, buf)


def split_meta(obj: dict) -> Tuple[dict, dict]:
    """Separate explanation metadata (unknown keys) from known fields."""
    known = {"id", "field", "value", "unit", "quantity_kind", "target_unit",
             "source", "record"}
    meta = {k: v for k, v in obj.items() if k not in known}
    return meta, {k: v for k, v in obj.items() if k in known}


class BatchProcessor:
    COMMIT_EVERY = 500

    def __init__(self, engine: ConversionEngine, store: storage.Store,
                 fields: Dict[str, "FieldRule"], *, run_id: str,
                 good_path: Optional[str], isolated_path: Optional[str]):
        self.engine = engine
        self.store = store
        self.fields = fields
        self.run_id = run_id
        self.good_f = open(good_path, "w", encoding="utf-8") if good_path else None
        self.iso_f = (open(isolated_path, "w", encoding="utf-8")
                      if isolated_path else None)
        self.good = 0
        self.isolated = 0
        self.roundtrip_failures = 0
        self._since_commit = 0

    def close(self) -> None:
        for f in (self.good_f, self.iso_f):
            if f is not None:
                f.close()
        self.good_f = self.iso_f = None
        self.store.conn.commit()

    def _maybe_commit(self) -> None:
        self._since_commit += 1
        if self._since_commit >= self.COMMIT_EVERY:
            self.store.conn.commit()
            self._since_commit = 0

    # -- output -----------------------------------------------------------
    def _write_json(self, handle, obj: dict) -> None:
        if handle is not None:
            handle.write(json.dumps(obj, sort_keys=True, ensure_ascii=False) + "\n")

    def _flush(self) -> None:
        if self.good_f is not None:
            self.good_f.flush()
        if self.iso_f is not None:
            self.iso_f.flush()

    # -- processing -------------------------------------------------------
    def process_line(self, line: ByteLine) -> None:
        raw_sha = self.store.add_raw_line(
            self.run_id, line.line_no, line.offset, line.raw, "measurement")
        try:
            text = line.raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            # Undecodable bytes are file-structure damage: we cannot even
            # recover the line's characters, so the package is incomplete.
            raise StructuralFileError(
                f"line {line.line_no} at byte {line.offset} is not valid UTF-8 "
                f"({exc}); the measurement file is structurally damaged") from None
        stripped = text.strip()
        if not stripped:
            self._isolate(line, raw_sha, CATEGORY_SYNTAX, "blank_line",
                          "blank line in the measurement file", text, {})
            return
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            # A bad JSON *line* is line-local and isolated.  Damage to the
            # overall file structure (e.g. a broken framing) is handled below.
            self._isolate(line, raw_sha, CATEGORY_SYNTAX, "invalid_json_line",
                          f"line is not valid JSON: {exc}", text, {})
            return
        if not isinstance(obj, dict):
            self._isolate(line, raw_sha, CATEGORY_SYNTAX, "record_not_object",
                          "measurement line must be a JSON object", text, {})
            return
        self._process_object(line, raw_sha, obj, text)

    def _process_object(self, line: ByteLine, raw_sha: str, obj: dict,
                        text: str) -> None:
        meta, known = split_meta(obj)
        field_name = known.get("field")
        if not isinstance(field_name, str) or not field_name:
            self._isolate(line, raw_sha, CATEGORY_SYNTAX, "missing_field",
                          "measurement needs a non-empty 'field' string",
                          text, {})
            return
        rule = self.fields.get(field_name)
        if rule is None:
            self._isolate(line, raw_sha, CATEGORY_KIND, "unknown_field",
                          f"no target rule for field {field_name!r}", text,
                          {"field": field_name})
            return
        kind = known.get("quantity_kind")
        if kind != rule.quantity_kind:
            self._isolate(
                line, raw_sha, CATEGORY_KIND, "quantity_kind_mismatch",
                f"line declares quantity_kind {kind!r} but field "
                f"{field_name!r} is bound to {rule.quantity_kind!r}; the input "
                f"is isolated instead of silently changing kind",
                text, {"field": field_name, "declared": kind,
                       "bound": rule.quantity_kind})
            return
        if "target_unit" in known and known["target_unit"] != rule.target_unit:
            self._isolate(
                line, raw_sha, CATEGORY_KIND, "target_unit_mismatch",
                f"line target_unit {known['target_unit']!r} differs from the "
                f"field rule target {rule.target_unit!r}",
                text, {"field": field_name,
                       "declared_target": known["target_unit"],
                       "rule_target": rule.target_unit})
            return
        unit = known.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            self._isolate(line, raw_sha, CATEGORY_SYNTAX, "missing_unit",
                          "measurement needs a non-empty 'unit' string", text,
                          {"field": field_name})
            return
        try:
            conversion = self.engine.convert(known.get("value"), unit,
                                             rule.quantity_kind,
                                             rule.target_unit)
        except ConversionProblem as exc:
            self._isolate(line, raw_sha, exc.category, exc.reason, exc.message,
                          text, {"field": field_name, **exc.evidence,
                                 "unit": unit,
                                 "quantity_kind": rule.quantity_kind,
                                 "target_unit": rule.target_unit})
            return

        explanation = self._explanation(line, raw_sha, obj, meta, conversion,
                                         field_name, rule)
        self.store.add_record(
            self.run_id, line.line_no, conversion, field=field_name,
            target_unit=rule.target_unit,
            explanation_json=json.dumps(explanation, sort_keys=True),
            raw_sha256=raw_sha)
        # Round-trip check: from the stored exact standard value and the saved
        # original unit/kind we must recover the original rational number.
        check = self.engine.reverse_check(
            conversion.value_text, conversion.source.text,
            conversion.source.kind, rule.target_unit,
            conversion.standard.str())
        if not check.get("matches"):
            self.roundtrip_failures += 1
        self.store.add_reverse_check(
            run_id=self.run_id, byte_line_no=line.line_no,
            unit_text=conversion.source.text, quantity_kind=conversion.source.kind,
            target_unit=rule.target_unit,
            standard_fraction=conversion.standard.str(), result=check)
        self.good += 1
        self._write_json(self.good_f, {
            "run_id": self.run_id,
            "byte_line_no": line.line_no,
            "field": field_name,
            "value": conversion.value_text,
            "unit": unit,
            "quantity_kind": rule.quantity_kind,
            "target_unit": rule.target_unit,
            "standard_value": conversion.standard.str(),
            "standard_num": str(conversion.standard.n),
            "standard_den": str(conversion.standard.d),
            "basic_value": conversion.basic_value.str(),
            "dimension": conversion.source.dimension,
            "source_multiplier": conversion.source.M.str(),
            "source_offset": conversion.source.C.str(),
            "raw_sha256": raw_sha,
        })
        self._flush()
        self._maybe_commit()

    def _isolate(self, line: ByteLine, raw_sha: str, category: str,
                 reason: str, message: str, text: str, evidence: dict) -> None:
        self.store.add_isolated(self.run_id, line.line_no, category, reason,
                                message, evidence, raw_sha)
        self.isolated += 1
        self._write_json(self.iso_f, {
            "run_id": self.run_id,
            "byte_line_no": line.line_no,
            "category": category,
            "reason": reason,
            "message": message,
            "evidence": evidence,
            "raw_sha256": raw_sha,
        })
        self._flush()
        self._maybe_commit()

    # -- explanations -----------------------------------------------------
    def _explanation(self, line: ByteLine, raw_sha: str, obj: dict,
                     meta: dict, c: Conversion, field_name: str,
                     rule) -> dict:
        prov = self.store.provenance(self.run_id, line.line_no)
        return {
            "byte_line_no": line.line_no,
            "byte_offset": prov["byte_offset"],
            "byte_len": prov["byte_len"],
            "raw_sha256": raw_sha,
            "raw_text": prov["raw_text"],
            "field": field_name,
            "quantity_kind": rule.quantity_kind,
            "target_unit": rule.target_unit,
            "source": {
                "value_text": c.value_text,
                "unit_expression": c.source.text,
                "parse_tree": c.source.ast,
                "normalised_unit": c.source.structure,
                "dimension_after_cancellation": c.source.dimension,
                "multiplier": c.source.M.str(),
                "offset": c.source.C.str(),
                "affine": c.source.affine,
            },
            "target": {
                "unit_expression": c.target.text,
                "parse_tree": c.target.ast,
                "normalised_unit": c.target.structure,
                "dimension_after_cancellation": c.target.dimension,
                "multiplier": c.target.M.str(),
                "offset": c.target.C.str(),
                "affine": c.target.affine,
            },
            "standard_value": {
                "num": str(c.standard.n), "den": str(c.standard.d),
                "fraction": c.standard.str(),
            },
            "basic_value": {
                "num": str(c.basic_value.n), "den": str(c.basic_value.d),
                "fraction": c.basic_value.str(),
            },
            "rules_applied": c.rules,
            "record_metadata": meta,
            "vendor_version": self.engine.vendor_version,
        }


@dataclass
class BatchResult:
    run_id: str
    status: str                     # "complete" | "incomplete"
    good: int
    isolated: int
    byte_count: int
    structural_error: Optional[str] = None
    categories: Dict[str, int] = field(default_factory=dict)
    roundtrip_failures: int = 0

    def json(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "good": self.good,
            "isolated": self.isolated,
            "byte_count": self.byte_count,
            "structural_error": self.structural_error,
            "categories": self.categories,
            "roundtrip_failures": self.roundtrip_failures,
        }


def prepare_run(conn, *, input_path: str, rules_path: str,
                vendor_path: Optional[str], engine: ConversionEngine,
                fields, run_id: Optional[str] = None) -> str:
    store = storage.Store(conn)
    rid = run_id or storage.new_run_id()
    store.start_run(
        rid,
        input_path=os.path.abspath(input_path),
        input_sha256=storage.sha256_file(input_path),
        rules_path=os.path.abspath(rules_path),
        rules_sha256=storage.sha256_file(rules_path),
        vendor_path=os.path.abspath(vendor_path) if vendor_path else None,
        vendor_sha256=storage.sha256_file(vendor_path) if vendor_path else None,
        vendor_version=engine.vendor_version,
        rules_json=json.dumps(
            {name: fields[name].json() for name in sorted(fields)},
            sort_keys=True),
    )
    return rid


def process_file(path: str, processor: BatchProcessor,
                 chunk_size: int = 65536) -> int:
    byte_count = 0
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        byte_count = f.tell()
    for line in iter_bytes_lines(path, chunk_size=chunk_size):
        processor.process_line(line)
    return byte_count


def run_batch(conn, *, input_path: str, rules_path: str,
              vendor_path: Optional[str], engine: ConversionEngine, fields,
              good_path: Optional[str] = None,
              isolated_path: Optional[str] = None,
              run_id: Optional[str] = None,
              chunk_size: int = 65536) -> BatchResult:
    store = storage.Store(conn)
    rid = prepare_run(conn, input_path=input_path, rules_path=rules_path,
                      vendor_path=vendor_path, engine=engine, fields=fields,
                      run_id=run_id)
    processor = BatchProcessor(engine, store, fields, run_id=rid,
                               good_path=good_path,
                               isolated_path=isolated_path)
    status = "complete"
    structural: Optional[str] = None
    byte_count = 0
    try:
        byte_count = process_file(input_path, processor,
                                  chunk_size=chunk_size)
    except StructuralFileError as exc:
        status, structural = "incomplete", exc.message
    except StructuralError as exc:
        status, structural = "incomplete", str(exc)
    finally:
        processor.close()
    categories = {}
    for row in conn.execute(
            "SELECT category, COUNT(*) AS n FROM isolated WHERE run_id=? "
            "GROUP BY category ORDER BY category", (rid,)):
        categories[row["category"]] = row["n"]
    store.finish_run(rid, status, good=processor.good,
                     isolated=processor.isolated, byte_count=byte_count,
                     structural_error=structural)
    return BatchResult(rid, status, processor.good, processor.isolated,
                       byte_count, structural, categories,
                       processor.roundtrip_failures)
