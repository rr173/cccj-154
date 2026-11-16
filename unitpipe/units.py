"""Parser for the published UCUM subset.

Grammar (also printed by ``unitpipe rules`` and kept in docs/grammar.md)::

    expr     := factor (("/" | ".") factor)*        # left-associative
    factor   := primary exponent?
    primary  := atom | "(" expr ")"
    exponent := DIGITS | "(" ["+" | "-"] DIGITS ")"
    atom     := SYMBOL | "[degF]" | "[" namespace ":" code "]"

* ``SYMBOL`` is a maximal word run resolved by longest legal unit symbol
  (``m`` metre, ``mg`` milligram, ``mm2`` square millimetre, ``m2`` square
  metre).  A defined symbol wins over a prefixed reading.
* Prefixes ``k`` (10^3), ``c`` (10^-2), ``m`` (10^-3), ``u`` (10^-6) apply to
  multiplicative units only, never to ``Cel``/``[degF]`` or vendor atoms.
* ``^`` and ``**`` are not in the subset; negative exponents need parentheses.
* Bracketed vendor atoms are lexically validated here but must resolve in the
  vendor dictionary at conversion time.
* Nothing is ever silently passed through: an unparsable unit is an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .defs import (MAX_CODE_LEN, MAX_EXPONENT, BuiltinDef, UnknownCodeError,
                   UnitSyntaxError, builtins)

NAMESPACE_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_]*\Z")
_CODE_IN_BRACKET_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_]*\Z")

BUILTIN_PREFIX = "builtin"


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AtomBuiltin:
    code: str                 # canonical builtin code, e.g. "m", "Cel", "[degF]"
    prefix: Optional[str]
    exponent: int
    text: str
    pos: int


@dataclass(frozen=True)
class AtomVendor:
    namespace: str
    code: str
    exponent: int
    text: str
    pos: int


@dataclass(frozen=True)
class Group:
    expr: object
    exponent: int
    pos: int


@dataclass(frozen=True)
class Op:
    op: str
    pos: int


@dataclass(frozen=True)
class Expr:
    items: Tuple[object, ...]
    pos: int = 0


def ast_text(node) -> str:
    if isinstance(node, AtomBuiltin):
        s = node.text
        return s
    if isinstance(node, AtomVendor):
        return node.text
    if isinstance(node, Group):
        return "(" + ast_text(node.expr) + ")" + _exp_text(node.exponent)
    if isinstance(node, Expr):
        out = []
        for it in node.items:
            out.append(it.op if isinstance(it, Op) else ast_text(it))
        return "".join(out)
    raise TypeError(node)


def _exp_text(e: int) -> str:
    return "" if e == 1 else f"({e})"


def ast_to_json(node) -> dict:
    if isinstance(node, AtomBuiltin):
        return {"kind": "atom", "symbol": node.code, "prefix": node.prefix,
                "exponent": node.exponent, "text": node.text, "pos": node.pos}
    if isinstance(node, AtomVendor):
        return {"kind": "vendor_atom", "namespace": node.namespace,
                "code": node.code, "exponent": node.exponent,
                "text": node.text, "pos": node.pos}
    if isinstance(node, Group):
        return {"kind": "group", "exponent": node.exponent, "pos": node.pos,
                "expr": ast_to_json(node.expr)}
    if isinstance(node, Expr):
        return {"kind": "expr", "pos": node.pos,
                "items": [{"kind": "op", "op": it.op, "pos": it.pos}
                          if isinstance(it, Op) else ast_to_json(it)
                          for it in node.items]}
    raise TypeError(node)


@dataclass(frozen=True)
class AtomRef:
    """A flattened atom with its effective signed exponent and block context."""

    kind: str                 # "builtin" | "vendor"
    code: str                 # builtin canonical code (builtin only)
    namespace: Optional[str]
    prefix: Optional[str]
    exponent: int
    text: str
    pos: int
    in_compound: bool         # appears together with other atoms


@dataclass
class ParsedUnit:
    text: str
    ast: object
    atoms: Tuple[AtomRef, ...]

    def has_atoms(self) -> bool:
        return bool(self.atoms)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class UnitParser:
    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.depth = 0

    def _skip_ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n\f\v":
            self.i += 1

    def _peek(self) -> str:
        self._skip_ws()
        return self.s[self.i] if self.i < len(self.s) else ""

    def _error(self, msg: str, pos: Optional[int] = None) -> UnitSyntaxError:
        p = self.i if pos is None else pos
        return UnitSyntaxError(msg, p, self.s[p:p + 12])

    # -- entry -----------------------------------------------------------
    def parse(self) -> Expr:
        if not isinstance(self.s, str):
            raise UnitSyntaxError("unit expression must be a string")
        if "\x00" in self.s:
            raise UnitSyntaxError("NUL byte in unit expression",
                                  self.s.index("\x00"))
        self.i = 0
        if self._peek() == "":
            raise UnitSyntaxError("empty unit expression", 0)
        node = self._parse_expr()
        if self._peek() != "":
            raise self._error(f"unexpected character {self.s[self.i]!r}")
        return node

    def _parse_expr(self) -> Expr:
        start = self.i
        items: List[object] = [self._parse_factor()]
        while True:
            c = self._peek()
            if c not in (".", "/"):
                break
            pos = self.i
            self.i += 1
            nxt = self._peek()
            if nxt in ("", ".", "/", ")"):
                raise self._error("operator without operand", pos)
            items.append(Op(c, pos))
            items.append(self._parse_factor())
        return Expr(tuple(items), start)

    def _parse_factor(self):
        node = self._parse_primary()
        exp, pos = self._parse_exponent_opt()
        if exp is None:
            return node
        if isinstance(node, AtomBuiltin):
            return AtomBuiltin(node.code, node.prefix, node.exponent * exp,
                               node.text, node.pos)
        if isinstance(node, AtomVendor):
            return AtomVendor(node.namespace, node.code, node.exponent * exp,
                              node.text, node.pos)
        if isinstance(node, Group):
            return Group(node.expr, node.exponent * exp, node.pos)
        raise self._error("invalid exponent target", pos)

    def _parse_primary(self):
        c = self._peek()
        pos = self.i
        if c == "(":
            self.i += 1
            self.depth += 1
            if self.depth > 64:
                raise self._error("group nesting too deep", pos)
            if self._peek() == ")":
                raise self._error("empty group", self.i)
            inner = self._parse_expr()
            if self._peek() != ")":
                raise self._error("unbalanced '('", pos)
            self.i += 1
            self.depth -= 1
            return Group(inner, 1, pos)
        if c == "[":
            return self._parse_bracket_atom()
        if c and c.isascii() and c.isalpha():
            return self._parse_word_atom()
        raise self._error("expected a unit symbol or '('", pos)

    def _parse_word_atom(self) -> AtomBuiltin:
        pos = self.i
        while (self.i < len(self.s) and self.s[self.i].isascii()
               and (self.s[self.i].isalnum() or self.s[self.i] == "_")):
            self.i += 1
        run = self.s[pos:self.i]
        # Longest legal unit symbol: the whole run, else head+digits exponent.
        try:
            d, prefix, exponent = builtins().resolve_word(run)
        except UnitSyntaxError as exc:
            raise UnitSyntaxError(exc.message, pos + exc.pos, run) from None
        return AtomBuiltin(d.code, prefix, exponent, run, pos)

    def _parse_bracket_atom(self):
        pos = self.i
        end = self.s.find("]", pos + 1)
        if end < 0:
            raise self._error("unbalanced '['", pos)
        inner = self.s[pos + 1:end]
        self.i = end + 1
        if ":" in inner:
            ns, code = inner.split(":", 1)
            if not NAMESPACE_RE.match(ns) or len(ns) > MAX_CODE_LEN:
                raise UnitSyntaxError(f"bad vendor namespace {ns!r}", pos, inner)
            if not _CODE_IN_BRACKET_RE.match(code) or len(code) > MAX_CODE_LEN:
                raise UnitSyntaxError(f"bad vendor code {code!r}", pos, inner)
            if ns == BUILTIN_PREFIX:
                d = _builtin_alias(code)
                if d is None:
                    raise UnknownCodeError(
                        f"unknown builtin unit [{inner}]", pos, inner)
                return AtomBuiltin(d.code, None, 1, f"[{inner}]", pos)
            return AtomVendor(ns, code, 1, f"[{inner}]", pos)
        if inner == "degF":
            return AtomBuiltin("[degF]", None, 1, "[degF]", pos)
        if _CODE_IN_BRACKET_RE.match(inner):
            # [Cel] is not a legal UCUM spelling of Cel; only degF is bracketed.
            raise UnknownCodeError(
                f"unknown or unsupported bracketed unit code [{inner}]", pos, inner)
        raise UnknownCodeError(f"bad bracketed unit code [{inner}]", pos, inner)

    def _parse_exponent_opt(self) -> Tuple[Optional[int], int]:
        c = self._peek()
        pos = self.i
        if c == "(":
            self.i += 1
            sign = 1
            if self._peek() in ("+", "-"):
                if self.s[self.i] == "-":
                    sign = -1
                self.i += 1
            if not self._peek().isdigit():
                raise self._error("exponent must be an integer", pos)
            val = self._parse_int()
            if self._peek() != ")":
                raise self._error("unbalanced exponent '(...)'", pos)
            self.i += 1
            return self._check_exp(sign * val, pos), pos
        if c.isdigit():
            val = self._parse_int()
            return self._check_exp(val, pos), pos
        return None, pos

    def _parse_int(self) -> int:
        pos = self.i
        while self.i < len(self.s) and self.s[self.i].isdigit():
            self.i += 1
        return int(self.s[pos:self.i])

    def _check_exp(self, e: int, pos: int) -> int:
        if e in (0, 1):
            raise self._error("exponent 0 and 1 are not allowed", pos)
        if abs(e) > MAX_EXPONENT:
            raise self._error(f"exponent too large (max {MAX_EXPONENT})", pos)
        return e


def _builtin_alias(code: str) -> Optional[BuiltinDef]:
    """Resolve ``[builtin:X]`` to a builtin unit, or None."""
    b = builtins()
    if code == "degF":
        return b.get("[degF]")
    if code in b.by_shell():
        return b.get(code)
    if code == "L":
        return b.get("L")
    return None


# ---------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------

def _flatten(node, sign: int, exp: int, out: List[Tuple[object, int]]) -> None:
    if isinstance(node, AtomBuiltin):
        out.append((node, sign * exp * node.exponent))
        return
    if isinstance(node, AtomVendor):
        out.append((node, sign * exp * node.exponent))
        return
    if isinstance(node, Group):
        _flatten(node.expr, sign, exp * node.exponent, out)
        return
    if isinstance(node, Expr):
        pending = 1
        for it in node.items:
            if isinstance(it, Op):
                pending = -1 if it.op == "/" else 1
                continue
            _flatten(it, sign * pending, exp, out)
            pending = 1
        return
    raise TypeError(node)


def parse_unit_text(text: str) -> ParsedUnit:
    if not isinstance(text, str):
        raise UnitSyntaxError("unit expression must be a string")
    ast = UnitParser(text).parse()
    flat: List[Tuple[object, int]] = []
    _flatten(ast, 1, 1, flat)
    multi = len(flat) > 1
    atoms: List[AtomRef] = []
    for node, effective in flat:
        if isinstance(node, AtomBuiltin):
            atoms.append(AtomRef("builtin", node.code, None, node.prefix,
                                 effective, node.text, node.pos,
                                 multi or abs(effective) != 1))
        else:
            atoms.append(AtomRef("vendor", node.code, node.namespace, None,
                                 effective, node.text, node.pos,
                                 multi or abs(effective) != 1))
    return ParsedUnit(text, ast, tuple(atoms))


def atom_display(a: AtomRef) -> str:
    if a.kind == "vendor":
        return f"[{a.namespace}:{a.code}]"
    return f"[builtin:{a.text}]" if a.text.startswith("[") else a.text
