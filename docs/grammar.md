# Published UCUM subset and unit definition table

This file is the normative statement of what the pipeline accepts. The code
in `unitpipe/units.py` and `unitpipe/defs.py` implements exactly this; the
`rules` command prints the same table as JSON.

## 1. Grammar

```
expr     := factor (("/" | ".") factor)*         # left-associative
factor   := primary exponent?
primary  := atom | "(" expr ")"
exponent := DIGITS | "(" ["+" | "-"] DIGITS [")"]
atom     := SYMBOL | "[degF]" | "[" namespace ":" code "]"
SYMBOL   := longest legal builtin symbol, optionally prefixed
```

Notes:

* The operator chain is applied strictly left to right, so `m/s/kg` means
  `(m/s)/kg`. `.` and `/` bind equally; `/` negates only the factor that
  immediately follows it.
* Integer exponents are written directly after an atom or a group:
  `m2`, `mm3`, `(m/s)2`, `m(-2)`. Parentheses around a **negative** exponent
  are mandatory; `m-2` is a syntax error because `-` is not a token of the
  subset. Exponents `0` and `1` are rejected: `m0`, `m1`, and `m(1)` are all
  syntax errors.
* `^` and `**` are **not** in the subset. `m^2` and `m**2` are syntax errors;
  this is deliberate, since guessing between UCUM's `m2` and a caret form is
  exactly the kind of "parse it by swallowing errors" behaviour the pipeline
  must not do.
* Whitespace (space, tab, CR, LF, FF, VT) may appear between tokens; there is
  no implicit multiplication across whitespace. An empty or all-whitespace
  expression, a NUL byte, an unbalanced `(`/`[`, a leading or trailing
  operator, `m//s`, and an empty group `()` are syntax errors.
* A bare `1` is **not** a legal unit atom. Dimensionless results are written
  out by the parser for diagnostics but never accepted as input.

## 2. Lexical policy: longest legal unit symbol

A word atom is a maximal run `[A-Za-z][A-Za-z0-9_]*`. Resolution is:

1. If the whole run is a defined symbol, it is that symbol. This is what
   makes `m` the metre and `kg` the kilogram.
2. Otherwise the run is split into the **longest** head that is a defined
   symbol plus a trailing decimal exponent, giving `m2`, `mm3`, `mg`, etc.
3. Otherwise the run is split into a defined prefix plus a defined
   **unprefixed, multiplicative** symbol; the longest prefix wins. This is
   what makes `mg` the milligram rather than "metre-gram".
4. Otherwise it is an unknown code (`furlong`, `degF` without brackets,
   `dam`), which is an error. No code is ever guessed from a nearby spelling.

Because step 2 looks for the longest head that is *defined*, the exponent is
read only after the symbol: `mm2` is `(10^-3 m)^2`, not `m * m2`. There is no
implicit multiplication: `ms` is a single symbol (millisecond) if it were
defined, and it is **not** in this table, so `ms` is an unknown code and must
be written `m.s` for metre-second.

## 3. Prefixes

| prefix | factor | applies to |
|--------|--------|-----------|
| `k`    | 10^3   | multiplicative builtin symbols only |
| `c`    | 10^-2  | multiplicative builtin symbols only |
| `m`    | 10^-3  | multiplicative builtin symbols only |
| `u`    | 10^-6  | multiplicative builtin symbols only |

Prefixes do not apply to `Cel` or `[degF]` (affine units) and do not apply to
vendor atoms. `mCel`, `kCel`, and `[acme:mX]` are errors.

## 4. Builtin unit definition table

All factors and offsets are exact rationals; the internal representation is
`x_basic = M * x + C` with `M > 0`.

| code | quantity | basic dimension | factor `M` | offset `C` | affine | prefixable |
|------|----------|-----------------|-----------|------------|--------|-----------|
| `m`   | length          | length^1        | 1              | 0              | no  | yes |
| `s`   | time            | time^1          | 1              | 0              | no  | yes |
| `g`   | mass            | mass^1          | 1              | 0              | no  | yes |
| `L`   | volume          | length^3        | 1/1000         | 0              | no  | yes |
| `K`   | temperature     | temperature^1   | 1              | 0              | no  | yes |
| `Cel` | temperature     | temperature^1   | 1              | 5463/20 (=273.15) | yes | no |
| `[degF]` | temperature  | temperature^1   | 5/9            | 45967/180 (=459.67·5/9) | yes | no |

Therefore:

* `L = 10^-3 m^3`, and its dimension is anchored as `length^3`, not as a
  separate "volume" dimension. `1 L -> 1/1000 m3` exactly.
* `0 Cel -> 0 + 273.15 = 5463/20 K`.
* `32 [degF] -> (32 + 459.67)·5/9 = 491.67·5/9 = 5463/20 K`.
* `1 mg/L = (10^-3 g) / (10^-3 m^3) = 1 g/m^3`.

`[degF]` is the only bracketed builtin symbol. `degF` without brackets is an
unknown code.

## 5. Dimensions

The basic dimensions are `length`, `mass`, `time`, `temperature` (in that
canonical ordering). Compound units carry exact rational exponents, so
`(m/s)2` and `m2/s2` are the same dimension `length^2.time^-2`. The predicate
"same dimension" is exact rational vector equality; there is no tolerance.

## 6. Quantity kinds

Every unit expression is evaluated under the declared `quantity_kind`:

| kind | shape allowed | arithmetic | offsets |
|------|---------------|------------|---------|
| `absolute` | exactly one atom at power 1 (may be parenthesised) | `y = M·x + C`, inverted for the target | applied |
| `delta`    | any grammar expression | `y = M·x` | never applied |
| `ratio`    | any grammar expression | `y = M·x` | forbidden; any offset unit is rejected regardless of value |

For `absolute` the target unit is inverted as `x = (y - C)/M`, so a target of
`Cel` works: `273.15 K -> 0 Cel`.

A compound or powered offset unit is a **kind error** in `absolute` even when
the dimensions cancel (`Cel.m/m`, `Cel2/Cel`, `Cel/s`). `delta` accepts
`Cel/s` and `[degF]/s` and interprets them multiplicatively (`18 [degF]/s ->
10 K/s`). `ratio` rejects `Cel` outright even when the value is `0`.

Absolute temperatures that map to a value below 0 K are a physical-range
error (`-300 Cel -> -537/20 K`). Negative deltas are fine.

## 7. Vendor atoms and the vendor dictionary

`[namespace:code]` denotes a vendor unit. `namespace` and `code` match
`[A-Za-z][A-Za-z0-9_]*` and are at most 64 characters. The namespace
`builtin` is reserved; `[builtin:degF]` is a spelling of `[degF]`, and
`[builtin:Cel]` of `Cel`. Redefining the builtin namespace is a structural
error, so an alias can never override a builtin meaning.

Vendor atoms only resolve against a declared vendor dictionary. An undeclared
`[acme:X]` is a syntax error, which is the same category as any other unknown
code.

## 8. Vendor relation file

JSON Lines, one object per line. Blank lines and lines starting with `#` are
ignored. Node declarations and relations are read in two passes, so node
lines may appear after the relations that use them.

### Node line

```json
{"type":"node","source":"acme","namespace":"acme","code":"X","dimension":"temperature"}
```

`dimension` must be one of `length`, `mass`, `time`, `temperature`. Node
identity is `(namespace, code)`. Two sources that use the same `namespace`
for different `source` values are a structural error: sources are never
merged. Two nodes in different namespaces may both be called `X`; they remain
distinct. Redeclaring the same `(namespace, code)` with the same dimension is
harmless; with a different dimension it is a structural error.

### Relation line

```json
{"type":"relation","source":"acme","relation_id":"r1",
 "u":{"namespace":"acme","code":"X"},
 "v":{"namespace":"builtin","code":"Cel"},
 "a":{"num":2,"den":1},"b":{"num":10,"den":1}}
```

Semantics: `x_v = a * x_u + b`, where `a` is a positive rational and `b` is
any rational. `a` and `b` are canonical `{"num": int, "den": int}` objects
with `den > 0`; JSON floats are rejected. Both endpoints must be declared
nodes with the **same** declared basic dimension, and the optional top-level
`dimension` must agree with them. A disagreement is a structural error for
the whole dictionary, not a per-line isolation.

Builtin endpoints are limited to `builtin:m`, `builtin:g`, `builtin:s`,
`builtin:K`, `builtin:Cel`, `builtin:degF`, because compound units are
constructed by the grammar, not by the relation file. The builtin relations
`Cel -> K` (`a=1, b=273.15`) and `degF -> K` (`a=5/9, b=45967/180`) enter the
same graph.

`relation_id` is unique per source; a duplicate with identical content is a
harmless redundancy, and a duplicate with different content is a structural
error.

## 9. Consistency, per kind and per component

The relation graph (vendor relations plus the two builtin relations) is split
into connected components. Within each component, a deterministic spanning
tree assigns every node an exact map to its basic unit. Then **every**
relation is checked against that assignment:

* `absolute`: the composed closed walk must be `a = 1, b = 0`.
* `delta` and `ratio`: the composed multiplier must be `a = 1`; offsets may
  disagree freely.

The tree-derived anchor of every builtin node in the component is also
checked against its builtin definition with the same per-kind rule. A
component with any failed check is **conflicted for that kind**, and every
unit in it is unusable for conversion in that kind, whether or not the
particular measured line happened to use a "convenient" edge. A component
with no builtin node at all is **unanchored** for every kind.

A check that fails yields a witness: the closed walk, each step's declared
relation with its own `a`/`b`, whether it was applied forwards or inversely,
the composed `a`/`b`, and whether that is consistent. The composed value is
the determinant of whether the edge is genuinely contradictory; it is not
selected from one path in preference to another, since every edge is checked.

States are cached under `(component, kind)`. `absolute` and `delta` never
share a cached transform; a conflict in `absolute` does not by itself
imply a conflict in `delta` (only a multiplier conflict does).

**Ordering.** All vendor relations must be loaded and checked before
conversion. Adding a relation later invalidates every cached unit
resolution and every analyzed component; the engine refuses to serve a stale
"ok" result. Node/relation order in the file does not affect which edges are
checked or the final state. Reversing a relation with its exact inverse
(`x_u = (x_v - b)/a`) is equivalent by construction.

## 10. Isolation categories

A bad measurement line is isolated as one of:

| category | meaning |
|----------|---------|
| `syntax_unsupported` | unknown code, grammar violation, missing/blank/bad decimal, blank line, JSON-object shape problem |
| `dimension_mismatch` | source and target dimensions differ |
| `kind_mismatch` | absolute used as a compound, offset unit used in a ratio, field-rule kind/target disagreement, unknown field |
| `invalid_range` | absolute temperature below 0 K, or a value that cannot be parsed as a decimal that would be required for a range check |
| `unit_conflict` | the unit's component is inconsistent for the declared kind |
| `unanchored` | the unit's component has no builtin anchor |

A structural failure of the whole package (invalid UTF-8 in the measurement
file, malformed vendor dictionary, malformed rules, dimension disagreement
inside a relation) aborts the run with `status = "incomplete"` and
`complete = 0` in the `runs` table. Per-line isolation never changes the
run status from `complete`.
