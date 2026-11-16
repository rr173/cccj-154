"""Vendor unit dictionaries, relation graphs, and per-kind consistency.

Vendor file format (JSON Lines; blank lines and ``#`` comments are ignored)::

    {"type":"node","source":"acme","namespace":"acme","code":"X",
     "dimension":"temperature"}

    {"type":"relation","source":"acme","relation_id":"r1",
     "u":{"namespace":"acme","code":"X"},
     "v":{"namespace":"builtin","code":"Cel"},
     "a":{"num":2,"den":1},
     "b":{"num":10,"den":1}}

``x_v = a * x_u + b`` with ``a > 0``, ``b`` any rational, both canonical
``{num,den}`` objects with ``den > 0``.  Node identity is ``(namespace, code)``;
a code alone never merges identities.  The builtin namespace is fixed to
``builtin`` and its relation endpoints are the basic-dimension units
``m, g, s, K, Cel, degF``; builtin units are anchored by the same relation
mechanism (``Cel -> K`` and ``degF -> K``).

Per quantity kind and per connected component the graph is checked by
assigning every node an exact affine map to its basic unit
``x_basic = M * x_node + C`` along a deterministic spanning tree, then:

* ``absolute``: every closed walk must compose to ``a = 1, b = 0``.
* ``delta`` and ``ratio``: every closed walk must have multiplier ``a = 1``
  (offsets are ignored and therefore may disagree freely).

A component with any unresolved contradiction is unusable for that kind as a
whole; components without a builtin node are ``unanchored``.  States are
computed independently per kind and cached under ``(component, kind)``.
"""

from __future__ import annotations

import json
import math
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .defs import BASIC_DIMS, MAX_CODE_LEN, StructuralError
from .rational import Q, Q0, Q1

NodeKey = Tuple[str, str]
BUILTIN_NS = "builtin"

_BUILTIN_PRIORITY = ("m", "g", "s", "K", "Cel", "degF")
_NAMESPACE_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_]*\Z")
_KINDS = ("absolute", "delta", "ratio")

# Minimal builtin relation anchors: x_basic = M * x_node + C.
BUILTIN_ANCHORS: Dict[NodeKey, Tuple[str, Q, Q]] = {
    (BUILTIN_NS, "m"): ("length", Q1, Q0),
    (BUILTIN_NS, "g"): ("mass", Q1, Q0),
    (BUILTIN_NS, "s"): ("time", Q1, Q0),
    (BUILTIN_NS, "K"): ("temperature", Q1, Q0),
    (BUILTIN_NS, "Cel"): ("temperature", Q1, Q(27315, 100)),
    (BUILTIN_NS, "degF"): ("temperature", Q(5, 9), Q(45967, 180)),
}


def node_text(key: NodeKey) -> str:
    return f"{key[0]}:{key[1]}"


def _node_from_text(text: str) -> NodeKey:
    ns, _, code = text.partition(":")
    return (ns, code)


@dataclass(frozen=True)
class VendorNode:
    key: NodeKey
    source: str
    dimension: str
    line_no: int = 0


@dataclass(frozen=True)
class Relation:
    source: str
    relation_id: str
    u: NodeKey
    v: NodeKey
    a: Q
    b: Q
    origin: str                 # "builtin" or "vendor"
    line_no: int = 0
    raw_json: str = ""

    @property
    def key(self) -> str:
        return f"{self.source}/{self.relation_id}"

    def to_json(self) -> dict:
        return {
            "source": self.source,
            "relation_id": self.relation_id,
            "u": {"namespace": self.u[0], "code": self.u[1]},
            "v": {"namespace": self.v[0], "code": self.v[1]},
            "a": {"num": self.a.n, "den": self.a.d},
            "b": {"num": self.b.n, "den": self.b.d},
            "origin": self.origin,
        }

    def declared_text(self) -> str:
        return (f"x_({node_text(self.v)}) = {self.a.str()} * "
                f"x_({node_text(self.u)}) + {self.b.str()}")

    def lookup_hint(self) -> dict:
        return {
            "source": self.source,
            "relation_id": self.relation_id,
            "origin": self.origin,
            "u": node_text(self.u),
            "v": node_text(self.v),
            "a": self.a.str(),
            "b": self.b.str(),
            "declared": self.declared_text(),
        }


BUILTIN_RELATIONS: Tuple[Relation, ...] = (
    Relation(BUILTIN_NS, "builtin/Cel->K", (BUILTIN_NS, "Cel"), (BUILTIN_NS, "K"),
             Q1, Q(27315, 100), "builtin"),
    Relation(BUILTIN_NS, "builtin/degF->K", (BUILTIN_NS, "degF"),
             (BUILTIN_NS, "K"), Q(5, 9), Q(45967, 180), "builtin"),
)


class VendorDictionary:
    """All vendor nodes and relations plus the fixed builtin anchors."""

    def __init__(self) -> None:
        self.nodes: Dict[NodeKey, VendorNode] = {}
        for key, (dim, _m, _c) in BUILTIN_ANCHORS.items():
            self.nodes[key] = VendorNode(key, BUILTIN_NS, dim)
        self.relations: List[Relation] = list(BUILTIN_RELATIONS)
        self._namespace_source: Dict[str, str] = {}
        self._relation_index: Dict[Tuple[str, str], str] = {}
        self.vendor_line_count = 0
        self.version = 0

    def bump_version(self) -> None:
        self.version += 1

    # -- loading ---------------------------------------------------------
    def load_jsonl(self, lines: Iterable[Tuple[int, str]], path: str = "<vendor>"):
        # Two passes so node declarations may appear after the relations that
        # use them; relation order therefore never changes the outcome.
        entries: List[Tuple[int, str, dict]] = []
        for line_no, raw in lines:
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError as exc:
                raise StructuralError(
                    f"{path}:{line_no}: invalid JSON in vendor dictionary: {exc}") from None
            if not isinstance(obj, dict):
                raise StructuralError(f"{path}:{line_no}: vendor line must be an object")
            typ = obj.get("type")
            if typ not in ("node", "relation"):
                raise StructuralError(
                    f"{path}:{line_no}: vendor line type must be 'node' or "
                    f"'relation', got {typ!r}")
            entries.append((line_no, raw, obj))
        for line_no, raw, obj in entries:
            if obj.get("type") == "node":
                self._add_node(obj, line_no, path, raw)
                self.vendor_line_count += 1
        for line_no, raw, obj in entries:
            if obj.get("type") == "relation":
                self._add_relation(obj, line_no, path, raw)
                self.vendor_line_count += 1
        self.version += 1

    @staticmethod
    def _req_str(obj: dict, name: str, line_no: int, path: str) -> str:
        v = obj.get(name)
        if not isinstance(v, str):
            raise StructuralError(f"{path}:{line_no}: field {name!r} must be a string")
        return v

    @staticmethod
    def _check_name(value: str, what: str, line_no: int, path: str) -> None:
        if len(value) > MAX_CODE_LEN or not _NAMESPACE_RE.match(value):
            raise StructuralError(
                f"{path}:{line_no}: bad {what} {value!r} "
                f"(ASCII letter first, then letters/digits/underscore)")

    def _add_node(self, obj: dict, line_no: int, path: str, raw: str) -> None:
        source = self._req_str(obj, "source", line_no, path)
        ns = self._req_str(obj, "namespace", line_no, path)
        code = self._req_str(obj, "code", line_no, path)
        dim = self._req_str(obj, "dimension", line_no, path)
        self._check_name(ns, "namespace", line_no, path)
        self._check_name(code, "code", line_no, path)
        if ns == BUILTIN_NS:
            raise StructuralError(
                f"{path}:{line_no}: namespace 'builtin' is reserved and cannot be "
                f"redeclared (aliases may not override builtin meanings)")
        if dim not in BASIC_DIMS:
            raise StructuralError(
                f"{path}:{line_no}: vendor node dimension must be one of "
                f"{BASIC_DIMS}, got {dim!r}")
        owner = self._namespace_source.setdefault(ns, source)
        if owner != source:
            raise StructuralError(
                f"{path}:{line_no}: namespace {ns!r} already belongs to source "
                f"{owner!r}; sources are not merged")
        key = (ns, code)
        prev = self.nodes.get(key)
        if prev is not None:
            if prev.dimension != dim:
                raise StructuralError(
                    f"{path}:{line_no}: node {node_text(key)} redeclared with "
                    f"conflicting dimension {dim!r} (was {prev.dimension!r})")
            return
        self.nodes[key] = VendorNode(key, source, dim, line_no)

    @staticmethod
    def _node_obj(obj: dict, name: str, line_no: int, path: str) -> NodeKey:
        v = obj.get(name)
        if not isinstance(v, dict):
            raise StructuralError(
                f"{path}:{line_no}: relation endpoint {name!r} must be an object "
                f"with namespace and code")
        ns = v.get("namespace")
        code = v.get("code")
        if not isinstance(ns, str) or not isinstance(code, str):
            raise StructuralError(
                f"{path}:{line_no}: relation endpoint {name!r} needs string "
                f"namespace and code")
        VendorDictionary._check_name(ns, "namespace", line_no, path)
        VendorDictionary._check_name(code, "code", line_no, path)
        return (ns, code)

    @staticmethod
    def _rational(obj: dict, name: str, line_no: int, path: str) -> Q:
        v = obj.get(name)
        if not isinstance(v, dict):
            raise StructuralError(
                f"{path}:{line_no}: {name!r} must be a reduced rational object "
                f"{{'num': int, 'den': int}} with den > 0")
        n, d = v.get("num"), v.get("den")
        if (not isinstance(n, int) or isinstance(n, bool)
                or not isinstance(d, int) or isinstance(d, bool)):
            raise StructuralError(
                f"{path}:{line_no}: {name!r}.num and {name!r}.den must be integers "
                f"(floats are not accepted)")
        if d <= 0:
            raise StructuralError(
                f"{path}:{line_no}: {name!r}.den must be > 0, got {d}")
        if math.gcd(abs(n), d) != 1 and n != 0:
            raise StructuralError(
                f"{path}:{line_no}: {name!r} must be given in reduced form "
                f"(gcd(|num|, den) = 1), got {n}/{d}")
        if n == 0 and d != 1:
            raise StructuralError(
                f"{path}:{line_no}: zero must be written as 0/1, got {n}/{d}")
        return Q(n, d)

    def _add_relation(self, obj: dict, line_no: int, path: str, raw: str) -> None:
        source = self._req_str(obj, "source", line_no, path)
        rid = self._req_str(obj, "relation_id", line_no, path)
        if not rid:
            raise StructuralError(f"{path}:{line_no}: relation_id must not be empty")
        u = self._node_obj(obj, "u", line_no, path)
        v = self._node_obj(obj, "v", line_no, path)
        a = self._rational(obj, "a", line_no, path)
        b = self._rational(obj, "b", line_no, path)
        if a.sign() <= 0:
            raise StructuralError(
                f"{path}:{line_no}: relation multiplier a must be positive, got {a.str()}")
        dims = []
        for key in (u, v):
            node = self.nodes.get(key)
            if node is None:
                if key[0] == BUILTIN_NS:
                    raise StructuralError(
                        f"{path}:{line_no}: builtin relation endpoint "
                        f"{node_text(key)} is not a basic-dimension builtin unit "
                        f"(allowed: {', '.join('builtin:' + c for c in _BUILTIN_PRIORITY)})")
                raise StructuralError(
                    f"{path}:{line_no}: relation endpoint {node_text(key)} is not "
                    f"declared by any node line")
            if key[0] != BUILTIN_NS and node.source != source:
                raise StructuralError(
                    f"{path}:{line_no}: node {node_text(key)} belongs to source "
                    f"{node.source!r}, not {source!r}")
            dims.append(node.dimension)
        if dims[0] != dims[1]:
            raise StructuralError(
                f"{path}:{line_no}: relation endpoints have different declared "
                f"dimensions ({dims[0]!r} vs {dims[1]!r}); dictionary is inconsistent")
        declared_dim = obj.get("dimension")
        if declared_dim is not None:
            if declared_dim != dims[0]:
                raise StructuralError(
                    f"{path}:{line_no}: relation dimension {declared_dim!r} does "
                    f"not match endpoint dimensions {dims[0]!r}")
        signature = json.dumps(
            [source, rid, list(u), list(v), [a.n, a.d], [b.n, b.d]], sort_keys=True)
        prev = self._relation_index.get((source, rid))
        if prev is not None:
            if prev != signature:
                raise StructuralError(
                    f"{path}:{line_no}: relation {source}/{rid} redeclared with "
                    f"different content")
            return
        self._relation_index[(source, rid)] = signature
        self.relations.append(Relation(source, rid, u, v, a, b, "vendor",
                                       line_no, raw))

    # -- graph -----------------------------------------------------------
    def adjacency(self) -> Dict[NodeKey, List[Tuple[Relation, NodeKey]]]:
        adj: Dict[NodeKey, List[Tuple[Relation, NodeKey]]] = {
            k: [] for k in self.nodes}
        for idx, r in enumerate(self.relations):
            adj.setdefault(r.u, []).append((idx, r, r.v))
            adj.setdefault(r.v, []).append((idx, r, r.u))
        # Relation order must not influence which spanning tree is built, so
        # sort by a content key that does not include the file position.
        for k in adj:
            adj[k].sort(key=lambda t: (node_text(t[2]), t[1].source,
                                       t[1].relation_id))
        return adj

    def components(self):
        """Deterministic components: list of (component_id, sorted node keys)."""
        parent: Dict[NodeKey, NodeKey] = {k: k for k in self.nodes}

        def find(x: NodeKey) -> NodeKey:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: NodeKey, y: NodeKey) -> None:
            rx, ry = find(x), find(y)
            if rx != ry:
                if node_text(rx) < node_text(ry):
                    parent[ry] = rx
                else:
                    parent[rx] = ry

        for r in self.relations:
            union(r.u, r.v)
        groups: Dict[NodeKey, List[NodeKey]] = {}
        for k in self.nodes:
            groups.setdefault(find(k), []).append(k)
        ordered = sorted(groups.values(), key=lambda g: min(node_text(k) for k in g))
        return [(i, sorted(g, key=node_text)) for i, g in enumerate(ordered)]


# ---------------------------------------------------------------------------
# Per-kind analysis
# ---------------------------------------------------------------------------

@dataclass
class KindState:
    component_id: int
    kind: str
    state: str                       # "ok" | "conflict" | "unanchored"
    dimension: Optional[str]
    anchored: bool
    M: Dict[NodeKey, Q] = field(default_factory=dict)
    C: Dict[NodeKey, Q] = field(default_factory=dict)
    provenance: Dict[NodeKey, List[str]] = field(default_factory=dict)
    witnesses: List[dict] = field(default_factory=list)
    edges_checked: int = 0

    def json(self) -> dict:
        return {
            "component_id": self.component_id,
            "kind": self.kind,
            "state": self.state,
            "dimension": self.dimension,
            "anchored": self.anchored,
            "edges_checked": self.edges_checked,
            "anchors": {
                node_text(k): {"M": self.M[k].str(), "C": self.C[k].str()}
                for k in sorted(self.M, key=node_text)
            },
            "provenance": {node_text(k): v for k, v in
                           sorted(self.provenance.items(), key=lambda kv: node_text(kv[0]))},
            "witnesses": self.witnesses,
        }


def _apply(step: Tuple[Q, Q], value: Q) -> Q:
    return step[0] * value + step[1]


def _compose(steps: Sequence[Tuple[Q, Q]]) -> Tuple[Q, Q]:
    A, B = Q1, Q0
    for a, b in steps:
        A, B = a * A, a * B + b
    return A, B


class Analyzer:
    def __init__(self, dictionary: VendorDictionary):
        self.d = dictionary
        self.adj = dictionary.adjacency()
        self._components = dictionary.components()
        self._cache: Dict[Tuple[int, str], KindState] = {}
        self._edge_cache: Dict[Tuple[int, str], List[Tuple[int, Relation, NodeKey]]] = {}

    # -- public ----------------------------------------------------------
    def components(self):
        return self._components

    def component_of(self) -> Dict[NodeKey, int]:
        out = {}
        for cid, nodes in self._components:
            for n in nodes:
                out[n] = cid
        return out

    def state(self, component_id: int, kind: str) -> KindState:
        key = (component_id, kind)
        if key not in self._cache:
            self._cache[key] = self._analyze(component_id, kind)
        return self._cache[key]

    def state_for_node(self, node: NodeKey, kind: str) -> KindState:
        return self.state(self.component_of()[node], kind)

    def verify_all(self) -> None:
        """Analyze every component for every kind before any conversion.

        Loading a dictionary is not enough: the closure checks run here, so a
        contradiction anywhere is known before the first result is produced.
        """
        for cid, _nodes in self._components:
            for kind in _KINDS:
                self.state(cid, kind)

    # -- analysis --------------------------------------------------------
    def _edges(self, nodes: Sequence[NodeKey]):
        inside = set(nodes)
        out = []
        for idx, r in enumerate(self.d.relations):
            if r.u in inside and r.v in inside:
                out.append((idx, r, r.v))
        return out

    def _analyze(self, component_id: int, kind: str) -> KindState:
        nodes = dict(self._components)[component_id]
        edges = self._edges(nodes)
        dims = sorted({self.d.nodes[n].dimension for n in nodes})
        dimension = dims[0] if len(dims) == 1 else None
        builtin_nodes = [n for n in nodes if n[0] == BUILTIN_NS]
        anchored = bool(builtin_nodes)

        # Deterministic root: builtin nodes first in fixed priority.
        root: NodeKey
        if anchored:
            root = sorted(builtin_nodes,
                          key=lambda n: (_BUILTIN_PRIORITY.index(n[1])
                                         if n[1] in _BUILTIN_PRIORITY else 99,
                                         n[1]))[0]
        else:
            root = sorted(nodes, key=node_text)[0]

        M: Dict[NodeKey, Q] = {}
        C: Dict[NodeKey, Q] = {}
        prov: Dict[NodeKey, List[str]] = {root: []}
        parent: Dict[NodeKey, Tuple[NodeKey, int]] = {}

        if root[0] == BUILTIN_NS:
            M[root], C[root] = BUILTIN_ANCHORS[root][1], BUILTIN_ANCHORS[root][2]
        else:
            M[root], C[root] = Q1, Q0

        queue = deque([root])
        while queue:
            p = queue.popleft()
            for idx, r, other in self.adj.get(p, []):
                if other in M:
                    continue
                # Assign `other` from p using the relation's declared map.
                if r.u == p and r.v == other:
                    # x_other = a * x_p + b; keep the same basic-unit value
                    M[other] = M[p] / r.a
                    C[other] = C[p] - M[other] * r.b
                elif r.v == p and r.u == other:
                    # x_p = a * x_other + b  =>  x_other = (x_p - b)/a
                    M[other] = M[p] * r.a
                    C[other] = C[p] + M[p] * r.b
                else:
                    continue
                parent[other] = (p, idx)
                prov[other] = prov.get(p, []) + [r.key]
                queue.append(other)

        state = KindState(component_id, kind,
                          "ok" if anchored else "unanchored",
                          dimension, anchored)
        witnesses: List[dict] = []
        seen: set = set()
        edges_checked = 0

        def up_steps(n: NodeKey):
            """Steps mapping x_n -> x_root, each derived from a declared edge."""
            steps = []
            while n in parent:
                p, idx = parent[n]
                r = self.d.relations[idx]
                if r.u == n and r.v == p:
                    steps.append(((r.a, r.b), "forward", r, n, p))
                elif r.v == n and r.u == p:
                    steps.append(((Q1 / r.a, -r.b / r.a), "reverse", r, n, p))
                else:  # pragma: no cover - defensive
                    raise StructuralError("tree edge is not incident to child")
                n = p
            return steps

        def push_witness(w: dict) -> None:
            sig = json.dumps([w.get("kind"), w.get("conflict_edge"),
                              [[s["relation"]["source"], s["relation"]["relation_id"],
                                s["orientation"]] for s in w.get("steps", [])]],
                             sort_keys=True)
            if sig in seen:
                return
            seen.add(sig)
            witnesses.append(w)

        max_witnesses = 3
        # (1) Check every relation edge against the tree assignment.
        for idx, r, _other in edges:
            if r.u not in M or r.v not in M:
                continue
            edges_checked += 1
            Mu, Cu = M[r.u], C[r.u]
            Mv, Cv = M[r.v], C[r.v]
            # r says x_v = a*x_u + b, so x_u = (x_v - b)/a.  Propelled by the
            # already anchored x_v this predicts the anchor of u.
            exp_Mu = Mv * r.a
            exp_Cu = Cv + Mv * r.b
            mult_ok = Mu == exp_Mu
            abs_ok = mult_ok and Cu == exp_Cu
            ok = abs_ok if kind == "absolute" else mult_ok
            if ok or len(witnesses) >= max_witnesses:
                continue
            # Closed walk: u -> v along this edge, then v -> root -> u.
            seq = [((r.a, r.b), "forward", r, r.u, r.v)]
            for step, orient, rr, frm, to in up_steps(r.v):
                seq.append((step, orient, rr, frm, to))
            root_to_u = []
            for step, orient, rr, frm, to in up_steps(r.u):
                inv = (Q1 / step[0], -step[1] / step[0])
                inv_orient = "reverse" if orient == "forward" else "forward"
                root_to_u.append((inv, inv_orient, rr, to, frm))
            seq.extend(reversed(root_to_u))
            A, B = _compose([s[0] for s in seq])
            consistent = (A == Q1 and B == Q0) if kind == "absolute" else (A == Q1)
            steps_json = []
            for (sa, sb), orient, rr, frm, to in seq:
                steps_json.append({
                    "relation": rr.lookup_hint(),
                    "orientation": orient,
                    "from": node_text(frm),
                    "to": node_text(to),
                    "applied": {"a": sa.str(), "b": sb.str()},
                })
            w = {
                "kind": kind,
                "component_id": component_id,
                "dimension": dimension,
                "conflict_edge": r.lookup_hint(),
                "steps": steps_json,
                "composed": {"a": A.str(), "b": B.str()},
                "expected": {"a": "1", "b": "0" if kind == "absolute" else "*"},
                "consistent": consistent,
                "explanation": (
                    "absolute closed walks must compose to a=1,b=0"
                    if kind == "absolute" else
                    "delta/ratio closed walks must have multiplier a=1"),
            }
            push_witness(w)

        # (2) Anchor check: tree-derived anchor of every builtin node must
        #     agree with its declared builtin definition (same dimension).
        if anchored:
            for b in sorted(builtin_nodes, key=node_text):
                if b == root:
                    continue
                _dim_b, Mb, Cb = BUILTIN_ANCHORS[b]
                mult_ok = M[b] == Mb
                abs_ok = mult_ok and C[b] == Cb
                ok = abs_ok if kind == "absolute" else mult_ok
                if ok or len(witnesses) >= max_witnesses:
                    continue
                # cycle: b -> root through relations, root -> basic, basic -> b
                seq = []
                for step, orient, rr, frm, to in up_steps(b):
                    seq.append((step, orient, rr, frm, to))
                anchor_root = (M[root], C[root])
                seq.append((anchor_root, "anchor", None, root, ("basic", dimension or "?")))
                inv_anchor_b = (Q1 / Mb, -Cb / Mb)
                seq.append((inv_anchor_b, "anchor_inverse", None,
                            ("basic", dimension or "?"), b))
                A, B = _compose([s[0] for s in seq])
                consistent = (A == Q1 and B == Q0) if kind == "absolute" else (A == Q1)
                if not consistent:
                    steps_json = []
                    for (sa, sb), orient, rr, frm, to in seq:
                        entry = {
                            "orientation": orient,
                            "from": node_text(frm) if isinstance(frm, tuple) else str(frm),
                            "to": node_text(to) if isinstance(to, tuple) else str(to),
                            "applied": {"a": sa.str(), "b": sb.str()},
                        }
                        if rr is None:
                            entry["relation"] = {
                                "source": BUILTIN_NS,
                                "relation_id": f"builtin/anchor/{b[1] if orient == 'anchor_inverse' else root[1]}",
                                "origin": "builtin",
                                "declared": "builtin unit definition",
                            }
                        else:
                            entry["relation"] = rr.lookup_hint()
                        steps_json.append(entry)
                    push_witness({
                        "kind": kind,
                        "component_id": component_id,
                        "dimension": dimension,
                        "conflict_edge": {
                            "source": BUILTIN_NS,
                            "relation_id": f"builtin/anchor/{b[1]}",
                            "origin": "builtin",
                            "a": Mb.str(), "b": Cb.str(),
                            "declared": f"builtin anchor for {node_text(b)}",
                        },
                        "steps": steps_json,
                        "composed": {"a": A.str(), "b": B.str()},
                        "expected": {"a": "1", "b": "0" if kind == "absolute" else "*"},
                        "consistent": False,
                        "explanation": (
                            "builtin anchors must agree with every relation path"
                            + ("" if kind == "absolute" else " (multiplier only)")),
                    })

        if witnesses:
            state.state = "conflict"
        else:
            state.state = "ok" if anchored else "unanchored"
        state.M, state.C, state.provenance = M, C, prov
        state.witnesses = witnesses
        state.edges_checked = edges_checked
        return state


__all__ = [
    "VendorDictionary", "VendorNode", "Relation", "Analyzer", "KindState",
    "BUILTIN_ANCHORS", "BUILTIN_RELATIONS", "BUILTIN_NS", "node_text",
    "NodeKey",
]
