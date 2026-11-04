"""Deterministic validation gate for a proposed pattern (owner amendment 2026-11-04; addendum §3.6.2).

The gate decides whether a proposal *validates against the text*. It is the same for a synthesised
proposal (L4a) and an LLM proposal (L4b), and nothing in a proposal is executed except its regex,
which goes through the validator first. Checks, all required:

=================  ================================================================================
``re2_compile``    the validator (RE2 subset) passes and RE2 compiles the pattern. With
                   ``require_re2`` (default) a gate run on the ``regex`` engine fails here.
``lint``           the entry lint passes (groups, kinds, relations, PII shapes, ReDoS rules) and the
                   pattern has at least ``min_literal_chars`` literal characters (not over-broad).
``cited_examples`` every cited example id is a supplied evidence line (no invented examples); each
                   cited positive matches and each cited negative does not; the entry's own
                   ``examples`` are exactly the cited lines' text.
``no_regressions`` the pattern matches none of the held-out lines that other approved patterns of the
                   same kind match today (lines of an explicit ``fix`` target are exempt).
``min_support``    it matches at least ``min_support`` evidence lines, and at least that many
                   positives are cited.
``novelty``        its expanded regex is not already the regex of another entry.
=================  ================================================================================

The learning loop (L4b) supplies more evidence, which adds three checks (absent otherwise, so L4a
callers see the six checks above):

====================  =============================================================================
``cluster_coverage``  it matches at least ``min_cluster_coverage`` of the cluster's lines (the whole
                      cluster, not only the lines sent to the model).
``out_of_cluster``    it matches at most ``max_out_of_cluster`` (default 0) unparsed lines outside the
                      cluster: an absolute over-breadth bound.
``fix_target``        a ``fix`` matches every positive example of its target, and the target is an
                      approved entry (a human ``disable`` or ``deprecate`` wins over any fix).
====================  =============================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...extraction.patterns.engine import PatternEngine, PatternTimeout, literal_chars
from ...extraction.patterns.library import Library, LoadedEntry, PatternEntry, lint_entry, expand


@dataclass
class GateEvidence:
    lines: dict[str, str]  # evidence line id -> redacted line text (the cluster / support set)
    positive_ids: list[str]
    negative_ids: list[str] = field(default_factory=list)
    held_out: dict[str, list[str]] = field(default_factory=dict)  # approved pattern id -> lines it matches
    target_pattern_id: str | None = None  # a ``fix`` may match its target's lines
    cluster: dict[str, str] | None = None  # every line of the cluster (L4b ``cluster_coverage``)
    out_of_cluster: list[str] | None = None  # unparsed lines outside the cluster (L4b ``out_of_cluster``)


@dataclass
class GateCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class GateResult:
    checks: list[GateCheck]
    engine: str
    matched: int = 0

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def failed(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]

    def summary(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "engine": self.engine,
            "matched": self.matched,
            "checks": {c.name: {"passed": c.passed, **({"detail": c.detail} if c.detail else {})} for c in self.checks},
        }


def run_gate(
    entry: PatternEntry,
    evidence: GateEvidence,
    library: Library,
    engine: PatternEngine,
    *,
    min_support: int = 3,
    min_literal_chars: int = 4,
    require_re2: bool = True,
    min_cluster_coverage: float = 0.9,
    max_out_of_cluster: int = 0,
) -> GateResult:
    checks: list[GateCheck] = []
    regex, missing = expand(entry.regex, library.vocabularies, library.masks)
    result = GateResult(checks, engine.kind)

    # 1. compile -------------------------------------------------------------------------------
    if missing:
        checks.append(GateCheck("re2_compile", False, f"unresolvable templates {missing}"))
        return result
    if require_re2 and engine.kind != "re2":
        checks.append(GateCheck("re2_compile", False, "the gate requires the re2 engine (google-re2)"))
        return result
    try:
        compiled = engine.compile(regex)
    except Exception as exc:
        checks.append(GateCheck("re2_compile", False, f"{engine.kind} refuses the pattern: {exc}"))
        return result
    checks.append(GateCheck("re2_compile", True))

    # 2. lint ----------------------------------------------------------------------------------
    format_groups = {e.group for f in library.approved("format") for e in f.entry.entities}
    loaded = LoadedEntry(entry, "proposed", "gate", regex, "approved")
    issues = lint_entry(loaded, engine, min_literal_chars, format_groups, {})
    errors = [i for i in issues if i.severity == "error"]
    literals = literal_chars(regex)
    if errors:
        checks.append(GateCheck("lint", False, "; ".join(f"{i.code}: {i.message}" for i in errors[:5])))
        return result
    if literals < min_literal_chars:
        checks.append(GateCheck("lint", False, f"over-broad: {literals} literal characters (< {min_literal_chars})"))
        return result
    checks.append(GateCheck("lint", True))

    def matches(text: str) -> bool:
        return engine.search(compiled, text) is not None

    try:
        # 3. cited examples --------------------------------------------------------------------
        invented = [i for i in evidence.positive_ids + evidence.negative_ids if i not in evidence.lines]
        problems = []
        if invented:
            problems.append(f"cited ids not in the evidence: {invented[:5]}")
        else:
            problems += [f"positive {i} does not match" for i in evidence.positive_ids if not matches(evidence.lines[i])]
            problems += [f"negative {i} matches" for i in evidence.negative_ids if matches(evidence.lines[i])]
            cited_pos = sorted(evidence.lines[i] for i in evidence.positive_ids)
            cited_neg = sorted(evidence.lines[i] for i in evidence.negative_ids)
            if sorted(entry.examples.positive) != cited_pos or sorted(entry.examples.negative) != cited_neg:
                problems.append("the entry's examples are not exactly the cited evidence lines")
        checks.append(GateCheck("cited_examples", not problems, "; ".join(problems[:5])))

        # 4. no regressions on held-out lines of other approved patterns ------------------------
        stolen = []
        for pattern_id, lines in sorted(evidence.held_out.items()):
            if pattern_id == evidence.target_pattern_id:
                continue
            hits = sum(1 for text in lines if matches(text))
            if hits:
                stolen.append(f"{pattern_id}: {hits}")
        checks.append(GateCheck("no_regressions", not stolen, f"matches held-out lines of {stolen[:5]}" if stolen else ""))

        # 5. minimum support -------------------------------------------------------------------
        result.matched = sum(1 for text in evidence.lines.values() if matches(text))
        support_ok = result.matched >= min_support and len(evidence.positive_ids) >= min_support
        checks.append(
            GateCheck("min_support", support_ok, "" if support_ok else f"matched {result.matched} lines, cited {len(evidence.positive_ids)} (need {min_support})")
        )

        # 5b. learning-loop evidence (L4b) ------------------------------------------------------
        if evidence.cluster is not None:
            hits = sum(1 for text in evidence.cluster.values() if matches(text))
            share = hits / len(evidence.cluster) if evidence.cluster else 0.0
            ok = share >= min_cluster_coverage
            checks.append(GateCheck("cluster_coverage", ok, "" if ok else f"matches {hits}/{len(evidence.cluster)} cluster lines ({share:.2f} < {min_cluster_coverage})"))
        if evidence.out_of_cluster is not None:
            outside = sum(1 for text in evidence.out_of_cluster if matches(text))
            ok = outside <= max_out_of_cluster
            checks.append(GateCheck("out_of_cluster", ok, "" if ok else f"matches {outside} unparsed lines outside the cluster (> {max_out_of_cluster})"))
        if evidence.target_pattern_id is not None:
            target = library.entries.get(evidence.target_pattern_id)
            if target is None or target.status != "approved":
                status = "missing" if target is None else target.status
                checks.append(GateCheck("fix_target", False, f"target {evidence.target_pattern_id!r} is {status}"))
            else:
                missed = [t for t in target.entry.examples.positive if not matches(t)]
                checks.append(GateCheck("fix_target", not missed, f"misses {len(missed)} positive example(s) of the target" if missed else ""))
    except PatternTimeout as exc:
        checks.append(GateCheck("timeout", False, str(exc)))
        return result

    # 6. novelty -------------------------------------------------------------------------------
    duplicate = next((e.entry.id for e in library.entries.values() if e.regex == regex and e.entry.id != entry.id), None)
    checks.append(GateCheck("novelty", duplicate is None, f"same regex as {duplicate}" if duplicate else ""))
    return result


def run_configured_gate(entry: PatternEntry, evidence: GateEvidence, library: Library, engine: PatternEngine, ref: Any) -> GateResult:
    """``run_gate`` with the settings of ``extraction.pattern_library`` (``gate.min_support``,
    ``gate.require_re2``, ``min_literal_chars``): the entry point the learning loop uses."""
    return run_gate(
        entry,
        evidence,
        library,
        engine,
        min_support=ref.gate.min_support,
        min_literal_chars=ref.min_literal_chars,
        require_re2=ref.gate.require_re2,
        min_cluster_coverage=ref.gate.min_cluster_coverage,
        max_out_of_cluster=ref.gate.max_out_of_cluster,
    )
