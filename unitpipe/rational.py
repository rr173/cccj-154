"""Exact rational numbers (num/den, den > 0) for all unit conversions.

No binary floating point is used anywhere in this package.  Every amount,
factor, offset and result is a :class:`Fraction`.  Decimal input strings are
parsed with :meth:`Fraction.__truediv__` on integer digit strings, which is
exact.
"""

from __future__ import annotations

import math
import re
from fractions import Fraction
from typing import Optional

__all__ = ["Q", "Q0", "Q1", "parse_decimal", "q", "q_str", "q_atom", "num_str"]


class Q:
    """Minimal exact-rational wrapper around ``fractions.Fraction``.

    ``Q(a, b)`` is the reduced fraction ``a/b``; ``b`` may be negative and the
    sign is normalised onto the numerator.
    """

    __slots__ = ("n", "d")

    def __init__(self, n: int = 0, d: int = 1):
        if not isinstance(n, int) or isinstance(n, bool):
            raise TypeError("numerator must be int")
        if not isinstance(d, int) or isinstance(d, bool):
            raise TypeError("denominator must be int")
        if d == 0:
            raise ZeroDivisionError("rational denominator is zero")
        if d < 0:
            n, d = -n, -d
        g = math.gcd(abs(n), d)
        self.n = n // g
        self.d = d // g

    # -- construction ----------------------------------------------------
    @classmethod
    def from_fraction(cls, f: Fraction) -> "Q":
        return cls(f.numerator, f.denominator)

    @classmethod
    def from_int(cls, i: int) -> "Q":
        return cls(i, 1)

    # -- conversions -----------------------------------------------------
    def fraction(self) -> Fraction:
        return Fraction(self.n, self.d)

    def is_zero(self) -> bool:
        return self.n == 0

    def is_one(self) -> bool:
        return self.n == 1 and self.d == 1

    def sign(self) -> int:
        return 0 if self.n == 0 else (1 if self.n > 0 else -1)

    # -- arithmetic (exact) ---------------------------------------------
    def __add__(self, o) -> "Q":
        o = _c(o)
        return Q(self.n * o.d + o.n * self.d, self.d * o.d)

    def __radd__(self, o) -> "Q":
        return _c(o) + self

    def __sub__(self, o) -> "Q":
        o = _c(o)
        return Q(self.n * o.d - o.n * self.d, self.d * o.d)

    def __rsub__(self, o) -> "Q":
        return _c(o) - self

    def __mul__(self, o) -> "Q":
        o = _c(o)
        return Q(self.n * o.n, self.d * o.d)

    def __rmul__(self, o) -> "Q":
        return _c(o) * self

    def __truediv__(self, o) -> "Q":
        o = _c(o)
        if o.n == 0:
            raise ZeroDivisionError("division by zero rational")
        return Q(self.n * o.d, self.d * o.n)

    def __rtruediv__(self, o) -> "Q":
        return _c(o) / self

    def __neg__(self) -> "Q":
        return Q(-self.n, self.d)

    def __pow__(self, k: int) -> "Q":
        if not isinstance(k, int) or isinstance(k, bool):
            raise TypeError("exponent must be int")
        if k >= 0:
            return Q(self.n ** k, self.d ** k)
        if self.n == 0:
            raise ZeroDivisionError("zero to a negative power")
        return Q(self.d ** (-k), self.n ** (-k))

    def __eq__(self, o) -> bool:
        return isinstance(o, Q) and self.n == o.n and self.d == o.d

    def __hash__(self) -> int:
        return hash((self.n, self.d))

    def __lt__(self, o) -> bool:
        o = _c(o)
        return self.n * o.d < o.n * self.d

    def __le__(self, o) -> bool:
        o = _c(o)
        return self.n * o.d <= o.n * self.d

    def __gt__(self, o) -> bool:
        o = _c(o)
        return self.n * o.d > o.n * self.d

    def __ge__(self, o) -> bool:
        o = _c(o)
        return self.n * o.d >= o.n * self.d

    def __bool__(self) -> bool:
        return self.n != 0

    def __repr__(self) -> str:
        return f"Q({self.n}/{self.d})"

    # -- serialisation ---------------------------------------------------
    def str(self) -> str:
        """Canonical reduced form: ``"n"`` or ``"n/d"``."""
        if self.d == 1:
            return str(self.n)
        return f"{self.n}/{self.d}"

    def atom(self) -> str:
        """Canonical form used inside compound text (self-parenthesising)."""
        s = self.str()
        if self.d == 1 and self.n >= 0:
            return s
        return f"({s})"

    def num_str(self) -> str:
        return str(self.n)

    def den_str(self) -> str:
        return str(self.d)


Q0 = Q(0, 1)
Q1 = Q(1, 1)


def _c(o) -> Q:
    """Coerce int (and Q) to Q for mixed exact arithmetic."""
    if isinstance(o, Q):
        return o
    if isinstance(o, int) and not isinstance(o, bool):
        return Q(o, 1)
    return NotImplemented

# ASCII digits only: non-ASCII digits would be silently normalised.
_CANON_DEC_RE = re.compile(r"\A[+-]?[0-9]+(?:\.[0-9]*)?\Z|\A[+-]?\.[0-9]+\Z")


def parse_decimal(s: str) -> Optional[Q]:
    """Parse a finite signed decimal string exactly.

    Returns ``None`` for anything that is not a plain decimal number
    (empty, blank, exponent notation, NaN/Inf, embedded spaces, ...).
    """
    if not isinstance(s, str):
        return None
    # No trimming: surrounding whitespace is a different lexical token and
    # must not be silently accepted.
    t = s
    if not _CANON_DEC_RE.match(t):
        return None
    sign = 1
    if t[0] == "+":
        t = t[1:]
    elif t[0] == "-":
        sign = -1
        t = t[1:]
    if "." in t:
        ip, fp = t.split(".", 1)
    else:
        ip, fp = t, ""
    ip = ip or "0"
    digits = (ip + fp) or "0"
    den = 10 ** len(fp)
    return Q(sign * int(digits), den)


def q(s) -> Q:
    """Parse a canonical rational text (``"5/9"``, ``"-273/20"``, ``"3"``)."""
    if isinstance(s, Q):
        return s
    if isinstance(s, int):
        return Q(s, 1)
    if isinstance(s, str):
        m = re.match(r"\A([+-]?\d+)(?:/([+-]?\d+))?\Z", s.strip())
        if m:
            n = int(m.group(1))
            d = int(m.group(2)) if m.group(2) is not None else 1
            return Q(n, d)
    raise ValueError(f"not a canonical rational: {s!r}")


def q_str(v) -> str:
    if not isinstance(v, Q):
        v = q(v)
    return v.str()


def q_atom(v) -> str:
    if not isinstance(v, Q):
        v = q(v)
    return v.atom()


def num_str(v) -> str:
    return q(v).num_str()
