# unitpipe — design and operations

`unitpipe` normalises experiment result values with exact rational
arithmetic. It accepts a decimal original value, a unit expression, a
`quantity_kind`, and a target field unit, and produces:

* the standard value as a reduced fraction (`num`, `den`, and `text`),
* the exact conversion basis (multiplier, offset, dimension, per-atom factors
  and rules), and
* provenance back to the original record: run id, physical byte line number,
  byte offset and length, raw line SHA-256, and a SHA-256 of the input file.

It does **not** fit curves, propagate uncertainty, do statistics, or solve
anything scientifically. There is no binary floating point anywhere:
`fractions.Fraction` is used for value arithmetic, and the `Q` wrapper stores
canonical `num`/`den` integers.

## Layout

```
unitpipe/
  rational.py   exact rational Q, strict decimal parser
  defs.py       published grammar text, builtin unit definition table
  units.py      lexer, parser, AST, flattening to atoms
  vendor.py     vendor dictionary + per-kind component analysis
  engine.py     conversion semantics, caching, reverse checks, rules
  storage.py    SQLite schema and provenance helpers
  pipeline.py   byte-streaming normaliser and per-line isolation
  rules.py      rule-set loading and package-level checks
  cli.py        rules / normalize / explain / reverse / vendor / show / init
run.py          launcher (works under `python3 -I`)
tests/          standard-library unittest suite
docs/grammar.md normative grammar and unit table
```

## Quick start

```sh
python3 -I run.py init --db out.sqlite3

python3 -I run.py rules --rules examples/fields.json \
    --vendor examples/vendor_acme.jsonl

python3 -I run.py normalize --db out.sqlite3 \
    --rules examples/fields.json --vendor examples/vendor_acme.jsonl \
    --input examples/data/vendor_measurements.jsonl \
    --good good.jsonl --isolated isolated.jsonl

python3 -I run.py explain --db out.sqlite3 --line 1
python3 -I run.py reverse --db out.sqlite3 --rules examples/fields.json --line 1
python3 -I run.py vendor  --rules examples/fields.json --vendor examples/vendor_acme.jsonl
python3 -I run.py show records --db out.sqlite3
```

`--db` may appear before the subcommand. `run.py` inserts its own directory
on `sys.path`, so `python3 -I` works without an installed package and without
reading `sys.path[0]` from the current directory.

## Field rules file

```json
{
  "fields": {
    "T_abs":   {"quantity_kind": "absolute", "target_unit": "K",  "dimension": "temperature"},
    "T_delta": {"quantity_kind": "delta",    "target_unit": "K",  "dimension": "temperature"},
    "C_ratio": {"quantity_kind": "ratio",    "target_unit": "g/m3"}
  }
}
```

Each field is bound to exactly one `quantity_kind` and one target unit. A
measurement line that declares a different kind or a different target unit is
isolated (`kind_mismatch`); the pipeline never silently changes the kind.
`dimension` is optional and, if present, checked against the target unit's
actual dimension at load time. Rule problems are structural errors: a rules
file whose target unit cannot be parsed for its kind aborts the run.

## Measurement input

JSON Lines. The reader works on raw bytes and splits only on `\n`, so the
"line number" in every record is always the physical byte-line number, even
for blank or undecodable lines.

```json
{"field": "T_abs", "value": "0", "unit": "Cel", "quantity_kind": "absolute"}
```

* `value` must be a JSON **string** of a plain decimal (`0`, `-12.5`,
  `+3.`, `.5`, `00012.3400`). Numeric JSON values (`0.1`), exponent notation
  (`1e3`), `NaN`/`Inf`, `1,5`, and empty/blank strings are rejected. The error
  before any rounding is the point: a float literal has already lost the
  decimal text by the time Python sees it.
* `unit` must be a published-subset expression (see `docs/grammar.md`).
* `quantity_kind` must equal the field's bound kind.
* Optional `target_unit` must equal the field's target; it is a consistency
  assertion, not an instruction.
* `id`, `source`, `record` and unknown keys are preserved in the explanation.
* Blank lines, invalid JSON, and non-object lines are isolated line-by-line.
  Undecodable bytes make the whole run `incomplete`, because the physical
  structure of the file is damaged.

## Outputs

Each run writes:

1. rows in SQLite (`records`, `isolated`, `raw_lines`, `reverse_checks`,
   `vendor_*`, `field_rules`, `rule_checks`, `runs`), and
2. optionally two streaming JSONL files (`--good`, `--isolated`), flushed per
   line, committed to SQLite in batches.

A good record carries `standard_value` (`num`/`den`/`text`), `basic_value`,
`dimension`, `source_multiplier`, `source_offset`, `raw_sha256`, and the
physical `byte_line_no`. An isolated record carries `category`, `reason`,
`message`, `evidence`, and the same provenance.

`explain --line N` prints the full explanation: byte line, offset, length,
raw SHA-256, raw text, the source and target parse trees, the normalised
unit structures (including the exact multiplier and offset per atom), the
dimension after cancellation, the quantity-kind arithmetic formula, every
rule applied (builtin definitions, vendor relation paths with their exact
anchors and component state), and the standard and basic values as exact
fractions. For an isolated line it prints the category, reason, message,
evidence (including vendor conflict witnesses), and provenance.

`reverse` reconstructs the original rational value from the stored standard
value, the saved original unit expression and quantity kind, and the target
unit, then compares exact rationals. It does not compare formatted strings,
so `0.50` and `0.5` compare equal after round-trip. The comparison is
recorded in `reverse_checks`.

## SQLite schema notes

* Every rational is stored as text (`standard_num`, `standard_den`,
  `source_multiplier`, `a_num`, `b_num`, ...). No `REAL` column exists.
* `runs` records the input/rules/vendor paths and SHA-256 values, the vendor
  dictionary version, the status (`running`, `complete`, `incomplete`), and
  the structural error message if any.
* `raw_lines` stores the raw bytes' SHA-256, offset, length and decoded text
  for every physical line. `records.raw_sha256` and
  `isolated.raw_sha256` point back to it, and `runs.input_sha256` pins the
  whole input file.
* Vendor states are stored per `(component, kind)` in `vendor_analyses`, so a
  later audit can see exactly which kind was conflicted.

## Consistency guarantees

* A unit's structure is cached under `(vendor_version, kind, role, unit
  text)`. Kind and role are part of the key, so an `absolute` resolution can
  never be reused for a `delta` (or the other way around), and a target is
  never mistaken for a source. Changing the vendor dictionary bumps the
  version, clears the cache, rebuilds the analysis, and increments the
  analyzer generation; a late contradicting relation therefore revokes an
  earlier "ok".
* Vendor analysis is per kind. An `absolute` conflict does not automatically
  make `delta` conflicted; only a multiplier conflict does.
* A conflict anywhere in a component makes the whole component unusable for
  that kind. Builtin units whose dimension is anchored by a conflicted
  component are also refused for that kind, because the builtin anchor
  belongs to the same component.
* The relation file is read in two passes for node declarations, and the
  analyzer checks every edge against a deterministic spanning tree. File
  order does not affect the outcome; only whether an edge exists and its
  exact content.
* A relation whose declared endpoints have different basic dimensions is a
  structural error for the package, not a line-level isolation.

## Tests

```sh
python3 tests/run_tests.py
```

The suite uses only the standard library (`unittest`). Expected numbers are
hand-written literals in `tests/support.py` and the test modules; the
product's parser is never used to derive an expected value.
