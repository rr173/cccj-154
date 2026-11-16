"""SQLite persistence: provenance for every raw line, result or isolation.

Every stored number is text (canonical fraction); SQLite never sees a float.
Each normalised record carries ``(run_id, byte_line_no, raw_sha256)`` so it can
always be traced back to the exact original bytes it came from.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Iterable, Optional

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    input_path TEXT,
    input_sha256 TEXT,
    rules_path TEXT,
    rules_sha256 TEXT,
    vendor_path TEXT,
    vendor_sha256 TEXT,
    vendor_version INTEGER,
    rules_json TEXT,
    status TEXT NOT NULL,
    structural_error TEXT,
    good_count INTEGER NOT NULL DEFAULT 0,
    isolated_count INTEGER NOT NULL DEFAULT 0,
    byte_count INTEGER NOT NULL DEFAULT 0,
    complete INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS raw_lines (
    run_id TEXT NOT NULL,
    byte_line_no INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,
    byte_len INTEGER NOT NULL,
    raw_sha256 TEXT NOT NULL,
    raw_text TEXT,
    record_type TEXT NOT NULL,
    PRIMARY KEY (run_id, byte_line_no)
);

CREATE TABLE IF NOT EXISTS records (
    run_id TEXT NOT NULL,
    byte_line_no INTEGER NOT NULL,
    record_key TEXT NOT NULL,
    field TEXT NOT NULL,
    value_text TEXT NOT NULL,
    unit_text TEXT NOT NULL,
    quantity_kind TEXT NOT NULL,
    target_unit TEXT NOT NULL,
    standard_num TEXT NOT NULL,
    standard_den TEXT NOT NULL,
    standard_fraction TEXT NOT NULL,
    basic_num TEXT NOT NULL,
    basic_den TEXT NOT NULL,
    source_dimension TEXT NOT NULL,
    target_dimension TEXT NOT NULL,
    source_multiplier TEXT NOT NULL,
    source_offset TEXT NOT NULL,
    target_multiplier TEXT NOT NULL,
    target_offset TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    PRIMARY KEY (run_id, byte_line_no)
);

CREATE TABLE IF NOT EXISTS isolated (
    run_id TEXT NOT NULL,
    byte_line_no INTEGER NOT NULL,
    category TEXT NOT NULL,
    reason TEXT NOT NULL,
    message TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    PRIMARY KEY (run_id, byte_line_no)
);

CREATE TABLE IF NOT EXISTS reverse_checks (
    check_id TEXT PRIMARY KEY,
    run_id TEXT,
    byte_line_no INTEGER,
    unit_text TEXT NOT NULL,
    quantity_kind TEXT NOT NULL,
    target_unit TEXT NOT NULL,
    standard_fraction TEXT NOT NULL,
    original_fraction TEXT,
    recovered_fraction TEXT,
    matches INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS field_rules (
    run_id TEXT NOT NULL,
    field TEXT NOT NULL,
    quantity_kind TEXT NOT NULL,
    target_unit TEXT NOT NULL,
    dimension TEXT,
    resolved_dimension TEXT,
    multiplier TEXT,
    offset TEXT,
    PRIMARY KEY (run_id, field)
);

CREATE TABLE IF NOT EXISTS vendor_nodes (
    run_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    code TEXT NOT NULL,
    source TEXT NOT NULL,
    dimension TEXT NOT NULL,
    line_no INTEGER,
    PRIMARY KEY (run_id, namespace, code)
);

CREATE TABLE IF NOT EXISTS vendor_relations (
    run_id TEXT NOT NULL,
    source TEXT NOT NULL,
    relation_id TEXT NOT NULL,
    u_ns TEXT NOT NULL,
    u_code TEXT NOT NULL,
    v_ns TEXT NOT NULL,
    v_code TEXT NOT NULL,
    a_num TEXT NOT NULL,
    a_den TEXT NOT NULL,
    b_num TEXT NOT NULL,
    b_den TEXT NOT NULL,
    origin TEXT NOT NULL,
    line_no INTEGER,
    PRIMARY KEY (run_id, source, relation_id)
);

CREATE TABLE IF NOT EXISTS vendor_analyses (
    run_id TEXT NOT NULL,
    component_id INTEGER NOT NULL,
    quantity_kind TEXT NOT NULL,
    state TEXT NOT NULL,
    dimension TEXT,
    json TEXT NOT NULL,
    PRIMARY KEY (run_id, component_id, quantity_kind)
);

CREATE TABLE IF NOT EXISTS rule_checks (
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    PRIMARY KEY (run_id, name)
);

CREATE INDEX IF NOT EXISTS idx_isolated_category
    ON isolated (run_id, category);
CREATE INDEX IF NOT EXISTS idx_records_field
    ON records (run_id, field);
"""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def new_run_id() -> str:
    return uuid.uuid4().hex


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- runs -------------------------------------------------------------
    def start_run(self, run_id: str, *, input_path: Optional[str],
                  input_sha256: Optional[str], rules_path: Optional[str],
                  rules_sha256: Optional[str], vendor_path: Optional[str],
                  vendor_sha256: Optional[str], vendor_version: int,
                  rules_json: str) -> None:
        self.conn.execute(
            "INSERT INTO runs(run_id, created_at, input_path, input_sha256, "
            "rules_path, rules_sha256, vendor_path, vendor_sha256, "
            "vendor_version, rules_json, status, complete) "
            "VALUES (?,?,?,?,?,?,?,?,?,?, 'running', 0)",
            (run_id, now(), input_path, input_sha256, rules_path, rules_sha256,
             vendor_path, vendor_sha256, vendor_version, rules_json))
        self.conn.commit()

    def finish_run(self, run_id: str, status: str, *, good: int, isolated: int,
                   byte_count: int, structural_error: Optional[str] = None
                   ) -> None:
        self.conn.execute(
            "UPDATE runs SET status=?, good_count=?, isolated_count=?, "
            "byte_count=?, complete=?, structural_error=? WHERE run_id=?",
            (status, good, isolated, byte_count,
             1 if status == "complete" else 0, structural_error, run_id))
        self.conn.commit()

    def get_run(self, run_id: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,))
        return cur.fetchone()

    def latest_run(self) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT 1")
        return cur.fetchone()

    # -- raw lines --------------------------------------------------------
    def add_raw_line(self, run_id: str, byte_line_no: int, offset: int,
                     raw: bytes, record_type: str) -> str:
        digest = sha256_bytes(raw)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        self.conn.execute(
            "INSERT OR REPLACE INTO raw_lines(run_id, byte_line_no, "
            "byte_offset, byte_len, raw_sha256, raw_text, record_type) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, byte_line_no, offset, len(raw), digest, text, record_type))
        return digest

    # -- results ----------------------------------------------------------
    def add_record(self, run_id: str, byte_line_no: int, conversion, *,
                   field: str, target_unit: str, explanation_json: str,
                   raw_sha256: str) -> None:
        c = conversion
        self.conn.execute(
            "INSERT OR REPLACE INTO records(run_id, byte_line_no, record_key, "
            "field, value_text, unit_text, quantity_kind, target_unit, "
            "standard_num, standard_den, standard_fraction, basic_num, "
            "basic_den, source_dimension, target_dimension, source_multiplier, "
            "source_offset, target_multiplier, target_offset, rules_json, "
            "explanation_json, raw_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, byte_line_no,
             f"{run_id}:{byte_line_no}",
             field, c.value_text, c.source.text, c.source.kind, target_unit,
             str(c.standard.n), str(c.standard.d), c.standard.str(),
             str(c.basic_value.n), str(c.basic_value.d),
             c.source.dimension, c.target.dimension,
             c.source.M.str(), c.source.C.str(),
             c.target.M.str(), c.target.C.str(),
             json.dumps(c.rules, sort_keys=True), explanation_json,
             raw_sha256))

    def add_isolated(self, run_id: str, byte_line_no: int, category: str,
                     reason: str, message: str, evidence: dict,
                     raw_sha256: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO isolated(run_id, byte_line_no, category, "
            "reason, message, evidence_json, raw_sha256) VALUES (?,?,?,?,?,?,?)",
            (run_id, byte_line_no, category, reason, message,
             json.dumps(evidence, sort_keys=True, default=str), raw_sha256))

    def add_reverse_check(self, *, run_id: Optional[str],
                          byte_line_no: Optional[int], unit_text: str,
                          quantity_kind: str, target_unit: str,
                          standard_fraction: str, result: dict) -> str:
        cid = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO reverse_checks(check_id, run_id, byte_line_no, "
            "unit_text, quantity_kind, target_unit, standard_fraction, "
            "original_fraction, recovered_fraction, matches, detail_json, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, run_id, byte_line_no, unit_text, quantity_kind, target_unit,
             standard_fraction,
             (result.get("original") or {}).get("fraction"),
             (result.get("recovered") or {}).get("fraction"),
             1 if result.get("matches") else 0,
             json.dumps(result, sort_keys=True), now()))
        return cid

    # -- dictionary metadata ---------------------------------------------
    def add_field_rule(self, run_id: str, check: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO field_rules(run_id, field, quantity_kind, "
            "target_unit, dimension, resolved_dimension, multiplier, offset) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, check["field"], check["quantity_kind"],
             check["target_unit"], check.get("dimension"),
             check.get("resolved_dimension"), check.get("multiplier"),
             check.get("offset")))

    def add_rule_check(self, run_id: str, name: str, status: str, detail) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO rule_checks(run_id, name, status, "
            "detail_json) VALUES (?,?,?,?)",
            (run_id, name, status, json.dumps(detail, sort_keys=True, default=str)))

    def add_vendor_dictionary(self, run_id: str, vendor) -> None:
        for key, node in sorted(vendor.nodes.items()):
            self.conn.execute(
                "INSERT OR REPLACE INTO vendor_nodes(run_id, namespace, code, "
                "source, dimension, line_no) VALUES (?,?,?,?,?,?)",
                (run_id, key[0], key[1], node.source, node.dimension,
                 node.line_no))
        for r in vendor.relations:
            self.conn.execute(
                "INSERT OR REPLACE INTO vendor_relations(run_id, source, "
                "relation_id, u_ns, u_code, v_ns, v_code, a_num, a_den, "
                "b_num, b_den, origin, line_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, r.source, r.relation_id, r.u[0], r.u[1], r.v[0],
                 r.v[1], r.a.num_str(), r.a.den_str(), r.b.num_str(),
                 r.b.den_str(), r.origin, r.line_no))
        self.conn.commit()

    def add_vendor_analysis(self, run_id: str, state) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO vendor_analyses(run_id, component_id, "
            "quantity_kind, state, dimension, json) VALUES (?,?,?,?,?,?)",
            (run_id, state.component_id, state.kind, state.state,
             state.dimension, json.dumps(state.json(), sort_keys=True)))

    # -- queries ----------------------------------------------------------
    def record(self, run_id: str, byte_line_no: int) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM records WHERE run_id=? AND byte_line_no=?",
            (run_id, byte_line_no))
        return cur.fetchone()

    def isolated_row(self, run_id: str, byte_line_no: int
                     ) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM isolated WHERE run_id=? AND byte_line_no=?",
            (run_id, byte_line_no))
        return cur.fetchone()

    def raw_line(self, run_id: str, byte_line_no: int
                 ) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM raw_lines WHERE run_id=? AND byte_line_no=?",
            (run_id, byte_line_no))
        return cur.fetchone()

    def provenance(self, run_id: str, byte_line_no: int) -> dict:
        run = self.get_run(run_id)
        raw = self.raw_line(run_id, byte_line_no)
        if raw is None or run is None:
            raise KeyError(f"no provenance for {run_id}:{byte_line_no}")
        return {
            "run_id": run_id,
            "input_path": run["input_path"],
            "input_sha256": run["input_sha256"],
            "byte_line_no": raw["byte_line_no"],
            "byte_offset": raw["byte_offset"],
            "byte_len": raw["byte_len"],
            "raw_sha256": raw["raw_sha256"],
            "raw_text": raw["raw_text"],
        }
