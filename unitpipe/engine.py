"""Conversion engine: exact, kind-aware unit normalisation.

Semantics
---------
Every unit expression evaluates to an exact affine map on the basic
dimensions ``(length, mass, time, temperature)``::

    y_basic = M * x + C

``M > 0`` is rational, ``C`` is rational.  The ``quantity_kind`` decides how
the map is used:

``absolute``
    A point on one dimension.  Only a single atom (possibly parenthesised) is
    accepted; ``y = M*x + C`` and the target is inverted the same way.
    Offsets from ``Cel``/``[degF]`` apply.  Compound units and powers are
    kind errors even when dimensions cancel.  Negative temperature in kelvin
    is a physical-range error.

``delta``
    A difference.  ``y = M*x`` and target inversion is a division; offsets are
    never added.  ``Cel/s`` and ``[degF]/s`` are legal and scale by 1 and 5/9
    respectively.

``ratio``
    A pure multiplicative ratio.  Same arithmetic as ``delta`` but offset
    units (``Cel``, ``[degF]``, and any vendor node whose anchored offset is
    nonzero) are rejected outright, regardless of the measured value.

Vendor atoms are resolved per kind through :mod:`unitpipe.vendor`.  A
component in ``unit_conflict`` or ``unanchored`` for the requested kind makes
the whole expression unusable; nothing falls back to a convenient path.

All arithmetic uses :class:`~unitpipe.rational.Q`; binary floats never enter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import units
from .defs import (BASIC_DIMS, StructuralError, UnknownCodeError,
                   UnitSyntaxError, builtins)
from .rational import Q, Q0, Q1, parse_decimal
from .vendor import (Analyzer, BUILTIN_NS, KindState, NodeKey,
                     VendorDictionary, node_text)

CATEGORY_SYNTAX = "syntax_unsupported"
CATEGORY_DIMENSION = "dimension_mismatch"
CATEGORY_KIND = "kind_mismatch"
CATEGORY_RANGE = "invalid_range"
CATEGORY_CONFLICT = "unit_conflict"
CATEGORY_UNANCHORED = "unanchored"

# Component whose builtin anchor each basic dimension hangs from.
_DIM_ANCHOR: Dict[str, NodeKey] = {
    "length": (BUILTIN_NS, "m"),
    "mass": (BUILTIN_NS, "g"),
    "time": (BUILTIN_NS, "s"),
    "temperature": (BUILTIN_NS, "K"),
}

KIND_ABSOLUTE = "absolute"
KIND_DELTA = "delta"
KIND_RATIO = "ratio"
QUANTITY_KINDS = (KIND_ABSOLUTE, KIND_DELTA, KIND_RATIO)


# ---------------------------------------------------------------------------
# Outcome records
# ---------------------------------------------------------------------------

class ConversionProblem(Exception):
    """A line-level problem; carries the isolate category and evidence."""

    def __init__(self, category: str, reason: str, message: str,
                 evidence: Optional[dict] = None):
        super().__init__(message)
        self.category = category
        self.reason = reason
        self.message = message
        self.evidence = evidence or {}


@dataclass
class ResolvedUnit:
    text: str
    role: str                     # "source" | "target"
    kind: str
    ast: dict
    structure: dict
    dimension: str
    dims: Dict[str, Q]
    M: Q                          # exact multiplier to the basic unit
    C: Q                          # exact offset to the basic unit
    affine: bool
    rules: List[dict] = field(default_factory=list)
    atoms: Tuple[units.AtomRef, ...] = ()

    def scale_text(self) -> str:
        return self.M.str()

    def offset_text(self) -> str:
        return self.C.str()

    def json(self) -> dict:
        return {
            "unit_expression": self.text,
            "role": self.role,
            "quantity_kind": self.kind,
            "structure": self.structure,
            "dimension": self.dimension,
            "multiplier": self.M.str(),
            "offset": self.C.str(),
            "affine": self.affine,
            "rules": self.rules,
        }


@dataclass
class Conversion:
    value_text: str
    value: Q
    source: ResolvedUnit
    target: ResolvedUnit
    standard: Q
    basic_value: Q
    rules: List[dict]

    def json(self) -> dict:
        return {
            "standard_value": {
                "num": str(self.standard.n),
                "den": str(self.standard.d),
                "fraction": self.standard.str(),
            },
            "source": self.source.json(),
            "target": self.target.json(),
            "rules": self.rules,
        }


# ---------------------------------------------------------------------------
# Rules for target fields
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldRule:
    name: str
    quantity_kind: str
    target_unit: str
    dimension: Optional[str] = None

    def json(self) -> dict:
        return {"field": self.name, "quantity_kind": self.quantity_kind,
                "target_unit": self.target_unit, "dimension": self.dimension}


def load_field_rules(obj, source: str = "<rules>") -> Dict[str, FieldRule]:
    if not isinstance(obj, dict):
        raise StructuralError(f"{source}: rules file must be a JSON object")
    fields = obj.get("fields")
    if not isinstance(fields, dict):
        raise StructuralError(f"{source}: rules file needs a 'fields' object")
    out: Dict[str, FieldRule] = {}
    for name, spec in fields.items():
        if not isinstance(name, str) or not name:
            raise StructuralError(f"{source}: field names must be non-empty strings")
        if not isinstance(spec, dict):
            raise StructuralError(f"{source}: field {name!r} rule must be an object")
        kind = spec.get("quantity_kind")
        unit = spec.get("target_unit")
        dim = spec.get("dimension")
        if kind not in QUANTITY_KINDS:
            raise StructuralError(
                f"{source}: field {name!r} quantity_kind must be one of "
                f"{QUANTITY_KINDS}, got {kind!r}")
        if not isinstance(unit, str) or not unit:
            raise StructuralError(f"{source}: field {name!r} target_unit must be a string")
        if dim is not None and dim not in BASIC_DIMS:
            raise StructuralError(
                f"{source}: field {name!r} dimension must be one of {BASIC_DIMS} "
                f"or null, got {dim!r}")
        out[name] = FieldRule(name, kind, unit, dim)
    return out


def check_field_rules(rules: Dict[str, FieldRule], engine: "ConversionEngine"
                      ) -> List[dict]:
    """Parse every target unit; structural problems raise StructuralError."""
    checks = []
    for name in sorted(rules):
        rule = rules[name]
        try:
            resolved = engine.resolve_unit(rule.target_unit, rule.quantity_kind,
                                           role="target")
        except ConversionProblem as exc:
            if exc.category in (CATEGORY_CONFLICT, CATEGORY_UNANCHORED):
                # The target is syntactically fine; the dictionary is not.
                # This is a verified property of the vendor data, reported by
                # run-time conversions, not a broken rules file.
                checks.append({**rule.json(),
                               "resolved_dimension": None,
                               "status": "unusable",
                               "category": exc.category,
                               "message": exc.message})
                continue
            raise StructuralError(
                f"rules: field {name!r} target unit {rule.target_unit!r} is not "
                f"usable for kind {rule.quantity_kind}: {exc.message}") from None
        if rule.dimension is not None:
            actual = resolved.dims.get(rule.dimension)
            if actual is None or actual.is_zero():
                raise StructuralError(
                    f"rules: field {name!r} declares dimension {rule.dimension!r} "
                    f"but target unit {rule.target_unit!r} has dimension "
                    f"{resolved.dimension}")
        checks.append({**rule.json(), "resolved_dimension": resolved.dimension,
                       "multiplier": resolved.M.str(), "offset": resolved.C.str()})
    return checks


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ConversionEngine:
    def __init__(self, vendor: Optional[VendorDictionary] = None):
        self.vendor = vendor or VendorDictionary()
        self.analyzer = Analyzer(self.vendor)
        self._vendor_version = self.vendor.version
        self._finalized = False
        self._resolve_cache: Dict[tuple, ResolvedUnit] = {}
        self._cache_stats = {"hits": 0, "misses": 0, "evictions": 0}
        self._analyzer_generation = 1

    # -- vendor lifecycle -------------------------------------------------
    def finalize_vendor(self) -> None:
        """Freeze the dictionary: all relations are loaded and checked."""
        self._refresh()
        self._finalized = True

    def _refresh(self) -> None:
        if self.vendor.version != self._vendor_version:
            # Any late relation may invalidate earlier paths: drop everything
            # kind- and role-keyed and rebuild analysis lazily.
            self.analyzer = Analyzer(self.vendor)
            self._vendor_version = self.vendor.version
            self._analyzer_generation += 1
            self._cache_stats["evictions"] += len(self._resolve_cache)
            self._resolve_cache.clear()

    def vendor_changed(self) -> None:
        """Call after mutating the vendor dictionary; revokes cached states."""
        self._finalized = False
        self._refresh()

    def require_final_vendor(self) -> None:
        if not self._finalized:
            raise RuntimeError(
                "vendor dictionary is not final: load and verify every vendor "
                "relation before converting")

    @property
    def vendor_version(self) -> int:
        self._refresh()
        return self._vendor_version

    def cache_info(self) -> dict:
        return dict(self._cache_stats)

    # -- unit resolution --------------------------------------------------
    def resolve_unit(self, text: str, kind: str, role: str) -> ResolvedUnit:
        if kind not in QUANTITY_KINDS:
            raise ConversionProblem(CATEGORY_KIND, "unknown_quantity_kind",
                                    f"quantity_kind must be one of {QUANTITY_KINDS}")
        if role not in ("source", "target"):
            raise ValueError(role)
        version = self.vendor_version
        # Kind and role are part of the key: absolute and delta never share a
        # cached transform, and a target never reuses a source transform.
        key = (version, kind, role, text)
        hit = self._resolve_cache.get(key)
        if hit is not None:
            self._cache_stats["hits"] += 1
            return hit
        self._cache_stats["misses"] += 1
        resolved = self._resolve_uncached(text, kind, role)
        self._resolve_cache[key] = resolved
        return resolved

    def _resolve_uncached(self, text: str, kind: str, role: str) -> ResolvedUnit:
        try:
            parsed = units.parse_unit_text(text)
        except UnknownCodeError as exc:
            raise ConversionProblem(
                CATEGORY_SYNTAX, "unknown_unit_code", exc.message,
                {"unit": text, "position": exc.pos,
                 "fragment": exc.fragment}) from None
        except UnitSyntaxError as exc:
            raise ConversionProblem(
                CATEGORY_SYNTAX, "unit_syntax", exc.message,
                {"unit": text, "position": exc.pos,
                 "fragment": exc.fragment}) from None

        if kind == KIND_ABSOLUTE:
            self._check_absolute_shape(parsed, text)

        if kind == KIND_RATIO:
            self._check_ratio_atoms(parsed, text)
        if kind == KIND_DELTA:
            self._check_delta_atoms(parsed, text)

        dims: Dict[str, Q] = {d: Q0 for d in BASIC_DIMS}
        M = Q1
        C = Q0
        rules: List[dict] = []
        affine = False
        term_list: List[dict] = []

        for atom in parsed.atoms:
            if atom.kind == "builtin":
                d = builtins().get(atom.code)
                if d is None:  # pragma: no cover - parser guarantees this
                    raise StructuralError(f"unknown builtin atom {atom.code!r}")
                self._builtin_component_guard(d, kind, text)
                pfac = builtins().prefix_factor(atom.prefix)
                factor = (d.scale * pfac) ** atom.exponent
                for dim, e in d.dims:
                    dims[dim] = dims[dim] + e * atom.exponent
                M = M * factor
                if kind == KIND_ABSOLUTE and atom.exponent == 1 and d.affine:
                    C = d.offset
                    affine = True
                term_list.append({
                    "atom": atom.text, "unit": d.code,
                    "prefix": atom.prefix, "exponent": atom.exponent,
                    "factor": factor.str(),
                })
                rules.append({
                    "rule": "builtin_unit_definition",
                    "unit": d.code,
                    "definition": _builtin_definition(d),
                    "applied_exponent": atom.exponent,
                    "prefix": atom.prefix,
                })
            else:
                if not atom.namespace:
                    raise StructuralError("vendor atom without a namespace")
                node = (atom.namespace, atom.code)
                state, node_M, node_C = self._vendor_node_transform(
                    node, kind, atom.text)
                for dim, e in state_dimension_map(state, self.vendor, node):
                    dims[dim] = dims[dim] + e * atom.exponent
                if kind == KIND_ABSOLUTE:
                    # single atom guaranteed by _check_absolute_shape
                    M = node_M
                    C = node_C
                    affine = not node_C.is_zero()
                else:
                    M = M * (node_M ** atom.exponent)
                term_list.append({
                    "atom": atom.text,
                    "vendor": f"[{node[0]}:{node[1]}]",
                    "exponent": atom.exponent,
                    "multiplier": node_M.str(),
                    "offset": node_C.str(),
                })
                rules.append({
                    "rule": "vendor_relation_chain",
                    "unit": f"[{node[0]}:{node[1]}]",
                    "component_id": state.component_id,
                    "quantity_kind": kind,
                    "anchored_multiplier": node_M.str(),
                    "anchored_offset": node_C.str(),
                    "path": state.provenance.get(node, []),
                    "component_state": state.json(),
                    "vendor_version": self._vendor_version,
                })

        dims = {d: v for d, v in dims.items() if not v.is_zero()}
        dimension = ".".join(f"{d}^{dims[d].str()}" for d in BASIC_DIMS
                             if d in dims) or "1"
        structure = {
            "kind": "affine" if affine else "multiplicative",
            "dimension": dimension,
            "multiplier": M.str(),
            "offset": C.str(),
            "terms": term_list,
        }
        if not affine and len(dims) == 1 and next(iter(dims.values())) == Q1:
            structure["single_basic_dimension"] = True
        return ResolvedUnit(
            text=text, role=role, kind=kind, ast=units.ast_to_json(parsed.ast),
            structure=structure, dimension=dimension, dims=dims,
            M=M, C=C, affine=affine, rules=rules, atoms=parsed.atoms,
        )

    # -- shape/kind guards ------------------------------------------------
    @staticmethod
    def _check_absolute_shape(parsed: units.ParsedUnit, text: str) -> None:
        if len(parsed.atoms) != 1:
            raise ConversionProblem(
                CATEGORY_KIND, "absolute_requires_single_unit",
                "absolute quantities need exactly one unit atom; compound "
                "units and products are not points on a scale",
                {"unit": text, "atoms": [units.atom_display(a) for a in parsed.atoms]})
        atom = parsed.atoms[0]
        if atom.exponent != 1:
            raise ConversionProblem(
                CATEGORY_KIND, "absolute_requires_first_power",
                "absolute quantities need the unit at the first power",
                {"unit": text, "exponent": atom.exponent})
        if atom.prefix is not None and atom.kind == "builtin":
            d = builtins().get(atom.code)
            if d is None or d.affine:
                raise ConversionProblem(
                    CATEGORY_KIND, "prefixed_offset_unit",
                    "offset units cannot be prefixed", {"unit": text})

    @staticmethod
    def _check_ratio_atoms(parsed: units.ParsedUnit, text: str) -> None:
        for atom in parsed.atoms:
            if atom.kind == "builtin":
                d = builtins().get(atom.code)
                if d is not None and d.affine:
                    raise ConversionProblem(
                        CATEGORY_KIND, "ratio_rejects_offset_unit",
                        f"ratio quantities accept only offset-free "
                        f"multiplicative units; {atom.text!r} is affine",
                        {"unit": text, "atom": atom.text})

    @staticmethod
    def _check_delta_atoms(parsed: units.ParsedUnit, text: str) -> None:
        # Delta accepts Cel/s and [degF]/s by multiplier, but a prefixed
        # offset unit has no defined delta meaning.
        for atom in parsed.atoms:
            if atom.kind == "builtin" and atom.prefix is not None:
                d = builtins().get(atom.code)
                if d is not None and d.affine:
                    raise ConversionProblem(
                        CATEGORY_KIND, "prefixed_offset_unit",
                        "offset units cannot be prefixed, not even for a "
                        "delta", {"unit": text, "atom": atom.text})

    # -- builtin contamination -------------------------------------------
    def _builtin_component_guard(self, d, kind: str, text: str) -> None:
        """Refuse builtin units in a dimension whose component is conflicted.

        Vendor relations share the graph with the builtin anchors, so a
        contradiction anywhere in the temperature (etc.) component makes every
        unit anchored to that component unusable for that kind.  A component
        that happens to be ``unanchored`` cannot contain a builtin anchor, so
        builtin units never inherit ``unanchored``.
        """
        for atom_dim, _exp in d.dims:
            anchor = _DIM_ANCHOR.get(atom_dim)
            if anchor is None:
                continue
            state = self.analyzer.state_for_node(anchor, kind)
            if state.state == "conflict":
                raise ConversionProblem(
                    CATEGORY_CONFLICT, "unit_conflict",
                    f"the {atom_dim} component containing "
                    f"{node_text(anchor)} is inconsistent for quantity kind "
                    f"{kind}; unit {d.code!r} is in that component and cannot "
                    f"be used for conversion",
                    {"component_id": state.component_id,
                     "quantity_kind": kind, "dimension": atom_dim,
                     "unit": text, "builtin_unit": d.code,
                     "witnesses": state.witnesses})

    # -- vendor -----------------------------------------------------------
    def _vendor_node_transform(self, node: NodeKey, kind: str, text: str
                               ) -> Tuple[KindState, Q, Q]:
        if node not in self.vendor.nodes:
            raise ConversionProblem(
                CATEGORY_SYNTAX, "unknown_vendor_unit",
                f"vendor unit {node_text(node)!r} is not declared in the vendor "
                f"dictionary", {"unit": text, "vendor_unit": node_text(node)})
        state = self.analyzer.state_for_node(node, kind)
        if state.state == "conflict":
            raise ConversionProblem(
                CATEGORY_CONFLICT, "unit_conflict",
                f"vendor component {state.component_id} is inconsistent for "
                f"quantity kind {kind}; the whole component is unusable",
                {"component_id": state.component_id, "quantity_kind": kind,
                 "dimension": state.dimension,
                 "vendor_unit": node_text(node),
                 "witnesses": state.witnesses})
        if state.state == "unanchored":
            raise ConversionProblem(
                CATEGORY_UNANCHORED, "unanchored_component",
                f"vendor component {state.component_id} has no builtin anchor",
                {"component_id": state.component_id, "quantity_kind": kind,
                 "vendor_unit": node_text(node)})
        if kind == KIND_RATIO and not state.C.get(node, Q0).is_zero():
            raise ConversionProblem(
                CATEGORY_KIND, "ratio_rejects_offset_unit",
                f"ratio quantities reject vendor unit {node_text(node)!r} "
                f"because its anchored offset is "
                f"{state.C[node].str()}, not 0",
                {"component_id": state.component_id,
                 "vendor_unit": node_text(node)})
        if node not in state.M:
            raise StructuralError(f"node {node_text(node)} missing from state")
        return state, state.M[node], state.C[node]

    # -- decimal ----------------------------------------------------------
    @staticmethod
    def parse_value(value_text) -> Q:
        if not isinstance(value_text, str):
            raise ConversionProblem(
                CATEGORY_SYNTAX, "value_not_string",
                "the decimal value must be a string; non-string values are "
                "rejected rather than coerced through binary floating point",
                {"value": repr(value_text)})
        v = parse_decimal(value_text)
        if v is None:
            raise ConversionProblem(
                CATEGORY_SYNTAX, "invalid_decimal",
                f"empty value or invalid decimal {value_text!r}",
                {"value": value_text})
        return v

    # -- conversion -------------------------------------------------------
    def convert(self, value_text, unit_text, quantity_kind, target_unit_text
                ) -> Conversion:
        self.require_final_vendor()
        value = self.parse_value(value_text)
        source = self.resolve_unit(unit_text, quantity_kind, "source")
        target = self.resolve_unit(target_unit_text, quantity_kind, "target")
        if source.dimension != target.dimension:
            raise ConversionProblem(
                CATEGORY_DIMENSION, "dimension_mismatch",
                f"source dimension {source.dimension} does not match target "
                f"dimension {target.dimension}",
                {"source_dimension": source.dimension,
                 "target_dimension": target.dimension,
                 "source": unit_text, "target": target_unit_text,
                 "quantity_kind": quantity_kind})

        if quantity_kind == KIND_ABSOLUTE:
            basic = source.M * value + source.C
            standard = (basic - target.C) / target.M
        else:
            basic = source.M * value
            standard = basic / target.M

        if quantity_kind == KIND_ABSOLUTE and "temperature" in source.dims:
            if basic < Q0:
                raise ConversionProblem(
                    CATEGORY_RANGE, "absolute_temperature_below_zero",
                    f"absolute temperature converts to {basic.str()} K, which "
                    f"is below 0 K", {"kelvin": basic.str()})

        rules = self._rules_for(source, target, quantity_kind)
        return Conversion(value_text, value, source, target, standard, basic,
                          rules)

    def _rules_for(self, source: ResolvedUnit, target: ResolvedUnit,
                   kind: str) -> List[dict]:
        out = [{"rule": "quantity_kind_arithmetic", "quantity_kind": kind,
                "formula": ("x_basic = M_source*x + C_source; "
                            "x_target = (x_basic - C_target)/M_target"
                            if kind == KIND_ABSOLUTE else
                            "x_basic = M_source*x; x_target = x_basic/M_target")}]
        out.extend({"unit_role": "source", **r} for r in source.rules)
        out.extend({"unit_role": "target", **r} for r in target.rules)
        return out

    # -- reverse ----------------------------------------------------------
    def reverse(self, standard: Q, unit_text: str, quantity_kind: str,
                target_unit_text: str) -> Q:
        self.require_final_vendor()
        source = self.resolve_unit(unit_text, quantity_kind, "source")
        target = self.resolve_unit(target_unit_text, quantity_kind, "target")
        if quantity_kind == KIND_ABSOLUTE:
            basic = target.M * standard + target.C
            return (basic - source.C) / source.M
        basic = target.M * standard
        return basic / source.M

    def reverse_check(self, value_text: str, unit_text: str, quantity_kind: str,
                      target_unit_text: str, standard_fraction: str) -> dict:
        original = self.parse_value(value_text)
        standard = _parse_fraction(standard_fraction)
        try:
            recovered = self.reverse(standard, unit_text, quantity_kind,
                                     target_unit_text)
        except ConversionProblem as exc:
            return {"matches": False, "reason": exc.reason,
                    "category": exc.category, "message": exc.message}
        return {
            "matches": recovered == original,
            "original": {"num": str(original.n), "den": str(original.d),
                         "fraction": original.str()},
            "recovered": {"num": str(recovered.n), "den": str(recovered.d),
                          "fraction": recovered.str()},
            "comparison": "exact rational equality (not string formatting)",
        }


def _parse_fraction(text: str) -> Q:
    if not isinstance(text, str):
        raise ValueError("standard fraction must be a string")
    from .rational import q as parse_q
    return parse_q(text)


def _builtin_definition(d) -> str:
    dim = d.dims[0][0]
    if dim == "temperature":
        if d.code == "Cel":
            return "x_K = 1*x_Cel + 273.15"
        if d.code == "[degF]":
            return "x_K = (5/9)*x_degF + 45967/180"
        if d.code == "K":
            return "x_K = 1*x_K + 0"
        return f"x_{dim} = {d.scale.str()}*x_{d.code} + {d.offset.str()}"
    if d.code == "L":
        return "x_m3 = (1/1000)*x_L; L = 10^-3 m^3"
    return f"x_{dim} = {d.scale.str()}*x_{d.code} + 0"


def state_dimension_map(state: KindState, vendor: VendorDictionary,
                        node: NodeKey) -> List[Tuple[str, Q]]:
    dim = vendor.nodes[node].dimension
    return [(dim, Q1)]


__all__ = [
    "ConversionEngine", "Conversion", "ResolvedUnit", "FieldRule",
    "load_field_rules", "check_field_rules", "ConversionProblem",
    "CATEGORY_SYNTAX", "CATEGORY_DIMENSION", "CATEGORY_KIND", "CATEGORY_RANGE",
    "CATEGORY_CONFLICT", "CATEGORY_UNANCHORED", "QUANTITY_KINDS",
    "KIND_ABSOLUTE", "KIND_DELTA", "KIND_RATIO",
]
