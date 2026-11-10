"""Compare candidate literal vocabularies on externally supplied normalized JSONL.

Rows: text, day, service, level, optional level_basis/http_status/count. Output is
aggregate only. No source/provider calls, classifier labels, precision or recall.
Phrase matching approximates source analyzer semantics; it does not execute ES.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re

NEGATED_FAILURE = re.compile(
    r"\b(?:errors?|failures?|failed|exceptions?)\s*[\"']?\s*[:=]\s*(?:0(?:\.0+)?|false|null|none)\b"
    r"|\bno\s+(?:errors?|failures?|exceptions?)\b",
    re.I,
)


class Vocabulary:
    def __init__(self, value):
        self.domains = defaultdict(set)
        for family in value["families"]:
            for phrase in family["phrases"]:
                self.domains[phrase.casefold()].add(family["id"])
        for phrase in value.get("context_phrases", []):
            self.domains[phrase.casefold()].add("recovery")
        self.severity_values = {
            v.casefold() for v in value.get("severity_values", ["ERROR", "FATAL", "CRITICAL"])
        }
        self.http_status_min = value.get("http_status_min", 500)
        self.http_status_max = value.get("http_status_max", 599)
        alternatives = "|".join(
            re.escape(p).replace(r"\ ", r"\s+") for p in sorted(self.domains, key=len, reverse=True)
        )
        if not alternatives:
            raise ValueError("vocabulary must contain phrases")
        # Lookahead counts overlapping phrases independently, once per row.
        self.regex = re.compile(r"(?=(?<![A-Za-z0-9_])(" + alternatives + r")(?![A-Za-z0-9_]))", re.I)

    def phrases(self, text):
        return {re.sub(r"\s+", " ", match.group(1).casefold()) for match in self.regex.finditer(text)}

    def select(self, text):
        phrases = self.phrases(text)
        failure = any(self.domains[p] - {"recovery"} for p in phrases)
        recovery = any("recovery" in self.domains[p] for p in phrases)
        return failure, recovery


class Comparison:
    def __init__(self, seed, proposed):
        self.policies = {"seed": Vocabulary(seed), "proposed": Vocabulary(proposed)}
        self.total = Counter()
        self.strata = defaultdict(Counter)

    def add(self, row):
        text = row["text"]
        weight = row.get("count", 1)
        if type(weight) is not int or weight < 1:
            raise ValueError("count must be a positive integer")
        level = (row.get("level") or "unknown").casefold()
        severity = level in {"error", "fatal", "critical"}
        structured = severity and row.get("level_basis") == "level_field"
        status = row.get("http_status")
        http_error = type(status) is int and 500 <= status <= 599
        high_confidence = severity or http_error
        values = {
            "heads": weight,
            "input_rows": 1,
            "missing_level": weight * (level == "unknown"),
            "supplied_error_level": weight * severity,
            "structured_source_error_level": weight * structured,
            "http_5xx": weight * http_error,
            "high_confidence_selector": weight * high_confidence,
            "zero_failure_phrase": weight * bool(NEGATED_FAILURE.search(text)),
        }
        selections = {}
        for name, policy in self.policies.items():
            failure, recovery = policy.select(text)
            policy_severity = level in policy.severity_values
            policy_http = type(status) is int and policy.http_status_min <= status <= policy.http_status_max
            selected = policy_severity or policy_http or failure
            selections[name] = selected
            values.update(
                {
                    name + "_lexical_any": weight * (failure or recovery),
                    name + "_failure_phrase": weight * failure,
                    name + "_recovery_context": weight * recovery,
                    name + "_selected": weight * selected,
                    name + "_severity_selector": weight * policy_severity,
                    name + "_http_selector": weight * policy_http,
                    name + "_structured_error_retained": weight * (selected and structured),
                    name + "_supplied_error_retained": weight * (selected and severity),
                    name + "_selected_zero_failure": weight
                    * (selected and bool(NEGATED_FAILURE.search(text))),
                }
            )
        values["seed_only_selected"] = weight * (selections["seed"] and not selections["proposed"])
        values["proposed_only_selected"] = weight * (selections["proposed"] and not selections["seed"])
        key = (row.get("day", "unknown"), row.get("service", "unknown"), level)
        self.total.update(values)
        self.strata[key].update(values)

    def report(self):
        def with_shares(counts):
            result = dict(counts)
            for name in self.policies:
                result[name + "_selected_fraction"] = (
                    counts[name + "_selected"] / counts["heads"] if counts["heads"] else None
                )
            return result

        return {
            "semantics": "Case-insensitive ASCII-boundary phrases, flexible whitespace; configured severity (default ERROR/FATAL/CRITICAL) or supplied integer HTTP status in configured range (default 500..599) union non-recovery phrases. Recovery is context only. Zero/no-failure phrases are reported, never whole-row suppression.",
            "policies": {
                name: {
                    "severity_values": sorted(p.severity_values),
                    "http_status_min": p.http_status_min,
                    "http_status_max": p.http_status_max,
                }
                for name, p in self.policies.items()
            },
            "limitations": "Selection volume and supplied-error retention only; no labels, precision, recall, causality, source completeness, ES analyzer equivalence or outage proof.",
            "totals": with_shares(self.total),
            "strata": [
                {"day": day, "service": service, "level": level, **with_shares(counts)}
                for (day, service, level), counts in sorted(self.strata.items())
            ],
        }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", required=True, type=Path)
    ap.add_argument("--proposed", required=True, type=Path)
    ap.add_argument("--records", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    comparison = Comparison(json.loads(args.seed.read_text()), json.loads(args.proposed.read_text()))
    with args.records.open() as stream:
        for line in stream:
            comparison.add(json.loads(line))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(comparison.report(), indent=2) + "\n")


if __name__ == "__main__":
    main()
