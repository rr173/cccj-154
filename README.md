# unitpipe

Exact, source-traceable unit normalisation for experiment result data
(Python 3 stdlib + SQLite).

* **No floats, ever.** Decimal input is a string; every factor, offset and
  result is a reduced exact rational (`num`/`den`).
* **Published grammar only.** A small UCUM subset (`m`, `s`, `g`, `L`, `K`,
  `Cel`, `[degF]`, prefixes `k`/`c`/`m`/`u`, `.` `/` `()` integer powers),
  longest-legal-symbol lexing, and hard errors for unknown codes. See
  [docs/grammar.md](docs/grammar.md).
* **Kind-aware.** `absolute`, `delta`, and `ratio` have separate arithmetic;
  offset units never sneak into products, ratios, or deltas.
* **Vendor relations, per kind.** `x_v = a*x_u + b` declarations are checked
  for exact consistency per component and per kind, with closed-walk
  witnesses; a conflict disables the whole component for that kind only.
* **Provable provenance.** Every result or isolation carries its run id,
  physical byte line number, byte offset, raw SHA-256, and input SHA-256.
* **Streaming.** Input may exceed memory; good and isolated records are
  written as each line is decided.

## Quick start

```sh
python3 -I run.py init --db out.sqlite3
python3 -I run.py rules --rules examples/fields.json
python3 -I run.py normalize --db out.sqlite3 --rules examples/fields.json \
    --input examples/data/measurements.jsonl --good good.jsonl --isolated iso.jsonl
python3 -I run.py explain --db out.sqlite3 --line 1
python3 -I run.py reverse --db out.sqlite3 --rules examples/fields.json --line 1
```

Full documentation: [docs/design.md](docs/design.md),
[docs/grammar.md](docs/grammar.md).

## Tests

```sh
python3 tests/run_tests.py
```
