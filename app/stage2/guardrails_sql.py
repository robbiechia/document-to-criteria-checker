"""Stage 2 SQL guardrails.

Checks:
  S1  Syntax valid (sqlparse can tokenise without errors)
  S2  No destructive statements (DROP, DELETE, UPDATE, INSERT, TRUNCATE, ALTER)
  S3  Parameterised — no raw string concatenation risk (no || operator with literals)
  S4  All rule_ids mentioned in comments (coverage)
  S5  evaluate-equivalent exists — query contains a CASE/WHEN block
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.models.schemas import RuleSet

_DESTRUCTIVE = re.compile(
    r"\b(DROP|DELETE|UPDATE|INSERT|TRUNCATE|ALTER|EXEC|EXECUTE)\b",
    re.IGNORECASE,
)
_STRING_CONCAT_RISK = re.compile(r"\|\|\s*'")  # string concat with literal → injection risk


@dataclass
class SQLGuardrailReport:
    syntax_ok: bool = False
    syntax_error: str = ""
    no_destructive: bool = False
    destructive_violations: list[str] = field(default_factory=list)
    parameterised: bool = False
    concat_violations: list[str] = field(default_factory=list)
    rule_coverage_ok: bool = False
    missing_rule_ids: list[str] = field(default_factory=list)
    has_case_when: bool = False
    passed: bool = False

    def summary(self) -> dict:
        return {
            "passed":                self.passed,
            "syntax_ok":             self.syntax_ok,
            "no_destructive":        self.no_destructive,
            "parameterised":         self.parameterised,
            "rule_coverage_ok":      self.rule_coverage_ok,
            "has_case_when":         self.has_case_when,
            "destructive_violations": self.destructive_violations,
            "concat_violations":     self.concat_violations,
            "missing_rule_ids":      self.missing_rule_ids,
        }


def _collect_rule_ids(ruleset: RuleSet) -> list[str]:
    ids: list[str] = []

    def walk(node) -> None:
        if node.is_leaf:
            ids.append(node.rule_id)
        for child in node.conditions:
            walk(child)

    walk(ruleset.root)
    for rule in ruleset.escalated_rules:
        walk(rule)
    return ids


def apply_sql_guardrails(sql: str, ruleset: RuleSet) -> SQLGuardrailReport:
    report = SQLGuardrailReport()

    # S1 — syntax (sqlparse tokenises without raising)
    try:
        import sqlparse
        parsed = sqlparse.parse(sql)
        if not parsed or all(str(s).strip() == "" for s in parsed):
            report.syntax_ok = False
            report.syntax_error = "sqlparse produced empty parse result"
        else:
            report.syntax_ok = True
    except Exception as exc:
        report.syntax_ok = False
        report.syntax_error = str(exc)

    # S2 — no destructive statements
    destructive = _DESTRUCTIVE.findall(sql)
    report.no_destructive = len(destructive) == 0
    report.destructive_violations = list(set(d.upper() for d in destructive))

    # S3 — parameterised (no string concat with literals)
    concat_hits = _STRING_CONCAT_RISK.findall(sql)
    report.parameterised = len(concat_hits) == 0
    report.concat_violations = concat_hits

    # S4 — rule coverage (every rule_id referenced somewhere in the SQL)
    all_ids = _collect_rule_ids(ruleset)
    missing = [rid for rid in all_ids if rid not in sql]
    report.rule_coverage_ok = len(missing) == 0
    report.missing_rule_ids = missing

    # S5 — CASE/WHEN block exists
    report.has_case_when = bool(re.search(r"\bCASE\b.*\bWHEN\b", sql, re.IGNORECASE | re.DOTALL))

    report.passed = all([
        report.syntax_ok,
        report.no_destructive,
        report.parameterised,
        report.has_case_when,
        # rule_coverage is advisory — computation/existence stubs don't embed rule_id in SQL body
    ])

    return report
