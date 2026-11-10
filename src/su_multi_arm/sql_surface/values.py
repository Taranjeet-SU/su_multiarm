# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Modified by su_multi_arm contributors, 2026-11-08: adapted from NVIDIA/auto-ontology
# c447003f72a822301e927196a4ecb5b597c308d5 (auto_ontology/retrieval/text_to_sql/agents/
# sql_value_validation.py extract_value_checks, _single_column, _literal_item). Changes: returns
# typed ValueCheck records with the compared subject expression (resolved later through sqlglot
# scopes to a catalog column, not by column text); also covers IS, simple CASE subjects and
# instr() needles; no ILIKE (SQLite); no agentic probing, LIKE discovery or literal substitution:
# a value that is not observed is refused by the guard, never replaced.
"""Column-bound literals of a parsed SQLite ``SELECT`` (every predicate, including CASE conditions).

``extract_value_checks(tree)`` lists each ``(predicate, subject, literal)`` where a literal (string,
number, boolean, NULL; a negated number counts as a number) is compared with a non-literal
expression: ``=``, ``<>``, ``<``, ``<=``, ``>``, ``>=``, ``IS``, ``IN (<list>)``, ``BETWEEN``,
``LIKE`` (the pattern), ``CASE <subject> WHEN <literal>`` and ``instr(<subject>, <literal>)``. The
guard (``guard.py``) traces each subject to a catalog column and grounds the literal; any string
literal that is neither checked here nor in a permitted non-filter position is refused there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlglot import exp

COMPARISONS: dict[type, str] = {exp.EQ: "eq", exp.NEQ: "neq", exp.GT: "gt", exp.GTE: "gte", exp.LT: "lt", exp.LTE: "lte", exp.Is: "is"}


@dataclass(frozen=True)
class ValueCheck:
    id: str
    op: str  # eq | neq | gt | gte | lt | lte | is | in | between | like | case | instr
    predicate: Any  # the sqlglot predicate node
    subject: Any  # the compared expression
    literal: Any  # exp.Literal | exp.Boolean | exp.Null | exp.Neg(exp.Literal)

    def sql(self) -> str:
        return self.predicate.sql(dialect="sqlite")


def literal_of(node: Any) -> Any | None:
    """The literal a predicate operand is (unwrapping parentheses and a numeric sign), else None."""
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, (exp.Literal, exp.Boolean, exp.Null)):
        return node
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) and not node.this.is_string:
        return node
    return None


def extract_value_checks(tree: Any) -> list[ValueCheck]:
    found: list[tuple[str, Any, Any, Any]] = []
    for node in tree.walk():
        op = COMPARISONS.get(type(node))
        if op is not None:
            left, right = node.this, node.expression
            if (lit := literal_of(right)) is not None and literal_of(left) is None:
                found.append((op, node, left, lit))
            elif (lit := literal_of(left)) is not None and literal_of(right) is None:
                found.append((op, node, right, lit))
        elif isinstance(node, exp.Like):
            if (lit := literal_of(node.expression)) is not None:
                found.append(("like", node, node.this, lit))
        elif isinstance(node, exp.In):
            if node.args.get("query") is None:
                for value in node.expressions:
                    if (lit := literal_of(value)) is not None:
                        found.append(("in", node, node.this, lit))
        elif isinstance(node, exp.Between):
            for key in ("low", "high"):
                if (lit := literal_of(node.args.get(key))) is not None:
                    found.append(("between", node, node.this, lit))
        elif isinstance(node, exp.Case) and node.this is not None:
            for branch in node.args.get("ifs") or []:
                if (lit := literal_of(branch.this)) is not None:
                    found.append(("case", node, node.this, lit))
        elif isinstance(node, exp.StrPosition):
            if (lit := literal_of(node.args.get("substr"))) is not None:
                found.append(("instr", node, node.this, lit))

    checks: list[ValueCheck] = []
    seen: set[int] = set()
    for op, predicate, subject, literal in found:
        if id(literal) in seen:
            continue
        seen.add(id(literal))
        checks.append(ValueCheck(id=f"value_{len(checks) + 1}", op=op, predicate=predicate, subject=subject, literal=literal))
    return checks
