"""Field predicates as written in policy/*.yaml, parsed once and evaluated many.

Eligibility in `policy/allowance_rules.yaml` is expressed as short field
predicates over a denormalised feature row -- `grade <= 13`,
`site.site_class not in ['hq', 'office']`.  Keeping them as *data* rather than
Python is what lets the generator, the rule engine and the Policy Explorer
screen all speak about the same clause, and lets an alert quote the clause that
was broken verbatim.

The grammar is deliberately tiny:

    <field> <op> <literal>

where `field` may be dotted (`site.hardship_tier`), `op` is one of
``== != >= <= > < in "not in"`` and `literal` is a Python literal
(``true``/``false`` are accepted as YAML spells them).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_CLAUSE_RE = re.compile(
    r"^\s*(?P<field>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
    r"\s+(?P<op>==|!=|>=|<=|>|<|not\s+in|in)\s+"
    r"(?P<literal>.+?)\s*$"
)

_OPS = frozenset({"==", "!=", ">=", "<=", ">", "<", "in", "not in"})


class ClauseError(ValueError):
    """Raised when a policy clause cannot be parsed or evaluated."""


def _parse_literal(text: str) -> Any:
    """Parse the right-hand side. YAML spells booleans lowercase; Python does not."""
    stripped = text.strip()
    if stripped in ("true", "false"):
        return stripped == "true"
    if stripped == "null":
        return None
    try:
        return ast.literal_eval(stripped)
    except (ValueError, SyntaxError) as exc:  # pragma: no cover - policy typo guard
        raise ClauseError(f"cannot parse literal {text!r}") from exc


def lookup(row: Mapping[str, Any], field: str) -> Any:
    """Resolve a possibly-dotted field against a flat-or-nested feature row.

    Flat keys win, because the detector's feature frame is a wide table where
    `site.hardship_tier` is literally a column name.
    """
    if field in row:
        return row[field]
    node: Any = row
    for part in field.split("."):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        else:
            raise ClauseError(f"feature row has no field {field!r}")
    return node


@dataclass(frozen=True)
class Predicate:
    """One parsed policy clause, kept alongside the text it came from."""

    field: str
    op: str
    literal: Any
    text: str

    @classmethod
    def parse(cls, text: str) -> Predicate:
        match = _CLAUSE_RE.match(text)
        if not match:
            raise ClauseError(f"cannot parse clause {text!r}")
        op = re.sub(r"\s+", " ", match.group("op"))
        if op not in _OPS:  # pragma: no cover - unreachable via the regex
            raise ClauseError(f"unsupported operator {op!r} in {text!r}")
        literal = _parse_literal(match.group("literal"))
        if op in ("in", "not in") and not isinstance(literal, (list, tuple, set)):
            raise ClauseError(f"{op!r} needs a list literal in {text!r}")
        return cls(field=match.group("field"), op=op, literal=literal, text=text.strip())

    def evaluate(self, row: Mapping[str, Any]) -> bool:
        """True when the row satisfies the clause.

        A NULL field never satisfies a clause: missingness is a data-quality
        issue, and paying an allowance on the strength of an absent field would
        be the generator inventing an entitlement.
        """
        value = lookup(row, self.field)
        if value is None:
            return False
        if self.op == "==":
            return bool(value == self.literal)
        if self.op == "!=":
            return bool(value != self.literal)
        if self.op == "in":
            return value in self.literal
        if self.op == "not in":
            return value not in self.literal
        try:
            if self.op == ">=":
                return bool(value >= self.literal)
            if self.op == "<=":
                return bool(value <= self.literal)
            if self.op == ">":
                return bool(value > self.literal)
            return bool(value < self.literal)
        except TypeError as exc:
            raise ClauseError(
                f"cannot compare {self.field}={value!r} with {self.literal!r}"
            ) from exc


@dataclass(frozen=True)
class ClauseSet:
    """An `all:` block -- every predicate must hold. An empty block is universal."""

    predicates: tuple[Predicate, ...]

    @classmethod
    def parse_all(cls, clauses: list[str] | None) -> ClauseSet:
        return cls(tuple(Predicate.parse(c) for c in (clauses or [])))

    @property
    def fields(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for predicate in self.predicates:
            seen.setdefault(predicate.field, None)
        return tuple(seen)

    def evaluate(self, row: Mapping[str, Any]) -> bool:
        return all(p.evaluate(row) for p in self.predicates)

    def failing(self, row: Mapping[str, Any]) -> tuple[Predicate, ...]:
        """The clauses a row breaks -- this is what an alert cites to a reviewer."""
        return tuple(p for p in self.predicates if not p.evaluate(row))
