"""Builtin unit definitions and shared definitions for the UCUM subset.

The builtin table is deliberately small and explicit; see docs/grammar.md for
the published grammar.  All scales and offsets are exact rationals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .rational import Q, Q1, q

BASIC_DIMS: Tuple[str, ...] = ("length", "mass", "time", "temperature")

PREFIXES: Dict[str, int] = {"k": 3, "c": -2, "m": -3, "u": -6}

MAX_EXPONENT = 9999
MAX_CODE_LEN = 64


class StructuralError(Exception):
    """File/dictionary structural damage: the whole package is incomplete."""


class UnitSyntaxError(Exception):
    def __init__(self, message: str, pos: int = 0, fragment: str = ""):
        super().__init__(message)
        self.message = message
        self.pos = pos
        self.fragment = fragment


class UnknownCodeError(UnitSyntaxError):
    """An unknown or unsupported unit code (never guessed)."""


class KindUnitError(Exception):
    """The unit parses but is not legal for the declared quantity kind."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


class UnitConflictError(Exception):
    """The vendor component has a contradiction for this quantity kind."""

    def __init__(self, message: str, evidence: List[dict]):
        super().__init__(message)
        self.message = message
        self.evidence = evidence


class UnanchoredError(Exception):
    def __init__(self, message: str, evidence: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.evidence = evidence or {}


BUILTIN_NODE_CODE: Dict[str, str] = {
    "m": "m",
    "g": "g",
    "s": "s",
    "K": "K",
    "Cel": "Cel",
    "degF": "[degF]",
}


@dataclass(frozen=True)
class BuiltinDef:
    code: str                      # canonical builtin code, e.g. "Cel" or "[degF]"
    node_code: Optional[str]       # vendor-node code ("degF"), None for L
    dims: Tuple[Tuple[str, Q], ...]
    scale: Q
    offset: Q
    affine: bool
    prefixable: bool
    dim_label: str

    @property
    def display(self) -> str:
        return self.code

    def dim_dict(self) -> Dict[str, Q]:
        return dict(self.dims)

    def dim_text(self) -> str:
        return ".".join(f"{d}^{e.str()}" for d, e in self.dims) or "1"


class BuiltinTable:
    def __init__(self) -> None:
        self._defs: Dict[str, BuiltinDef] = {}
        self._load()

    def _add(self, code: str, node_code: Optional[str],
             dims: Tuple[Tuple[str, Q], ...], scale, offset, affine: bool,
             prefixable: bool, dim_label: str) -> None:
        d = BuiltinDef(code, node_code, dims, q(scale), q(offset), affine,
                       prefixable, dim_label)
        if affine and offset == 0:
            raise StructuralError(f"affine unit {code} needs a nonzero offset")
        self._defs[code] = d

    def _load(self) -> None:
        T = (("temperature", Q1),)
        self._add("m", "m", (("length", Q1),), 1, 0, False, True, "length")
        self._add("g", "g", (("mass", Q1),), 1, 0, False, True, "mass")
        self._add("s", "s", (("time", Q1),), 1, 0, False, True, "time")
        self._add("K", "K", T, 1, 0, False, True, "temperature")
        self._add("Cel", "Cel", T, 1, Q(27315, 100), True, False, "temperature")
        # [degF]: x_K = (x + 459.67) * 5/9 = 5/9 x + 45967/180
        self._add("[degF]", "degF", T, Q(5, 9), Q(45967, 180), True, False,
                  "temperature")
        # L = 10^-3 m^3: a multiplicative unit of a derived dimension, still
        # anchored to the basic length dimension.
        self._add("L", None, (("length", Q(3, 1)),), Q(1, 1000), 0, False,
                  True, "volume")

    def get(self, code: str) -> Optional[BuiltinDef]:
        return self._defs.get(code)

    def all(self) -> List[BuiltinDef]:
        return [self._defs[c] for c in sorted(self._defs)]

    def by_shell(self) -> Dict[str, BuiltinDef]:
        """Symbols that can appear as word atoms in the grammar."""
        return {c: d for c, d in self._defs.items() if not c.startswith("[")}

    def node_dim(self, node_code: str) -> Optional[str]:
        for d in self._defs.values():
            if d.node_code == node_code:
                return d.dims[0][0] if len(d.dims) == 1 else None
        return None

    def node_transform(self, node_code: str) -> Optional[Tuple[Q, Q]]:
        for d in self._defs.values():
            if d.node_code == node_code:
                return d.scale, d.offset
        return None

    def resolve_word(self, text: str) -> Tuple[BuiltinDef, Optional[str], int]:
        """Resolve a maximal word run to (def, prefix, exponent).

        Longest legal unit symbol wins: the whole run first, then a split into
        a symbol head plus a decimal exponent tail.  A defined symbol is
        preferred over a prefixed interpretation.  Raises UnitSyntaxError.
        """
        if not text:
            raise UnknownCodeError("empty unit symbol", 0)
        for cut in range(len(text), 0, -1):
            head, tail = text[:cut], text[cut:]
            if tail and not tail.isdigit():
                continue
            hit = self._resolve_head(head)
            if hit is None:
                continue
            d, prefix = hit
            if not tail:
                return d, prefix, 1
            exponent = int(tail)
            if exponent in (0, 1):
                raise UnitSyntaxError(
                    "exponent 0 and 1 are not allowed by the grammar",
                    cut, text)
            if exponent > MAX_EXPONENT:
                raise UnitSyntaxError("exponent too large", cut, text)
            return d, prefix, exponent
        raise UnknownCodeError(f"unknown or unsupported unit code {text!r}", 0,
                               text)

    def _resolve_head(self, head: str) -> Optional[Tuple[BuiltinDef, Optional[str]]]:
        shell = self.by_shell()
        if head in shell:
            return shell[head], None
        # Longest prefix first; prefixes never apply to offset units.
        for prefix in sorted(PREFIXES, key=len, reverse=True):
            if not head.startswith(prefix):
                continue
            rest = head[len(prefix):]
            if not rest:
                continue
            d = shell.get(rest)
            if d is not None and d.prefixable and not d.affine:
                return d, prefix
        return None

    def prefix_factor(self, prefix: Optional[str]) -> Q:
        if prefix is None:
            return Q1
        return Q(10, 1) ** PREFIXES[prefix]


_BUILTINS = BuiltinTable()


def builtins() -> BuiltinTable:
    return _BUILTINS


GRAMMAR_TEXT = """\
expr     := factor (("/" | ".") factor)*        # left-associative
factor   := primary exponent?
primary  := atom | "(" expr ")"
exponent := DIGITS | "(" ["+"|"-"] DIGITS [")"]
atom     := SYMBOL | "[" namespace ":" code "]"   # vendor atom
          | "[degF]"                              # builtin Fahrenheit
SYMBOL   := longest legal builtin symbol, optionally with a prefix
            k=10^3, c=10^-2, m=10^-3, u=10^-6
"""
