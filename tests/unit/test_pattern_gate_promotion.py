"""Validation gate classes and the ``auto | manual`` promotion policy (owner amendment 2026-11-04)."""

from __future__ import annotations

import pytest
import yaml

from su_multi_arm.extraction.patterns.library import PatternEntry, load_library
from su_multi_arm.learning.patterns.gate import GateEvidence, run_gate
from su_multi_arm.learning.patterns.promotion import submit
from tests.pattern_fixtures import CONNECT, FORMAT, engine_or_skip, write_library

LINES = {f"u{i}": f"cache miss for key k{i} in {i * 3} ms" for i in range(6)}
LINES["n1"] = "cache hit for key k9 in 2 ms"
HELD_OUT = {"msg.dependency.connect_failed": ["db-client failed to connect redis", "queue-client refused by kafka"]}


def proposal(regex: str = r"^cache miss for key (?P<key>[a-z0-9]+) in (?P<ms>[0-9]+) ms$", kind: str = "message", positives=("u0", "u1", "u2"), negatives=("n1",)) -> PatternEntry:
    data = {
        "id": "syn.cache_miss",
        "kind": kind,
        "status": "proposed",
        "regex": regex,
        "fields": {"ms": {"type": "integer"}},
        "examples": {"positive": [LINES[i] for i in positives], "negative": [LINES[i] for i in negatives]},
        "provenance": {"author": "system:synthesis"},
    }
    if kind == "message":
        data["signature"] = {"id": "syn.cache_miss"}
    return PatternEntry.model_validate(data)


def evidence(positives=("u0", "u1", "u2"), negatives=("n1",), **extra) -> GateEvidence:
    return GateEvidence(lines=dict(LINES), positive_ids=list(positives), negative_ids=list(negatives), held_out=dict(HELD_OUT), **extra)


@pytest.fixture
def re2():
    return engine_or_skip("re2")


@pytest.fixture
def library(tmp_path, re2):
    return load_library(write_library(tmp_path, [FORMAT, CONNECT]), re2)


def test_a_valid_proposal_passes_every_check(library, re2):
    result = run_gate(proposal(), evidence(), library, re2)
    assert result.passed and result.matched == 6
    assert set(result.summary()["checks"]) == {"re2_compile", "lint", "cited_examples", "no_regressions", "min_support", "novelty"}


@pytest.mark.parametrize(
    ("entry", "ev", "failed"),
    [
        (lambda: proposal(r"^cache (?P<a>\S+)\S+ ms$"), lambda: evidence(), "lint"),  # ReDoS
        (lambda: proposal(r"(?P<a>a)(?P=a)"), lambda: evidence(), "re2_compile"),
        (lambda: proposal(r"^(?P<x>\S+) (?P<y>\S+)"), lambda: evidence(), "lint"),  # over-broad
        (lambda: proposal(), lambda: evidence(positives=("u0", "u1", "zz")), "cited_examples"),  # invented id
        (lambda: proposal(positives=("u0", "u1")), lambda: evidence(), "cited_examples"),  # examples != cited
        (lambda: proposal(negatives=("u3",)), lambda: evidence(negatives=("u3",)), "lint"),  # its own negative matches
        (lambda: proposal(r"^(?:cache miss for key (?P<key>[a-z0-9]+) in (?P<ms>[0-9]+) ms|[a-z]+-client (?P<v>refused by) kafka)$"), lambda: evidence(), "no_regressions"),
        (lambda: proposal(positives=("u0",)), lambda: evidence(positives=("u0",)), "min_support"),
    ],
)
def test_the_gate_rejects_each_class(library, re2, entry, ev, failed):
    result = run_gate(entry(), ev(), library, re2)
    assert not result.passed and failed in result.failed()


def test_the_gate_rejects_a_duplicate_regex(tmp_path, re2):
    existing = proposal().model_dump(mode="json", exclude_none=True) | {"id": "msg.cache", "status": "approved", "priority": 10, "signature": {"id": "cache"}}
    library = load_library(write_library(tmp_path, [FORMAT, CONNECT, existing]), re2)
    result = run_gate(proposal(), evidence(), library, re2)
    assert result.failed() == ["novelty"] and "msg.cache" in result.checks[-1].detail


def test_the_gate_requires_re2(library):
    regex_engine = engine_or_skip("regex")
    result = run_gate(proposal(), evidence(), library, regex_engine)
    assert result.failed() == ["re2_compile"] and "re2" in result.checks[0].detail


def test_a_fix_may_match_its_target_lines(library, re2):
    fix = proposal(r"^(?:cache miss for key (?P<key>[a-z0-9]+) in (?P<ms>[0-9]+) ms|[a-z]+-client (?P<v>refused by) kafka)$")
    result = run_gate(fix, evidence(target_pattern_id="msg.dependency.connect_failed"), library, re2)
    assert "no_regressions" not in result.failed()  # the target's lines are exempt
    # L4b: a fix must also keep every positive example of its target (this one drops two of them).
    assert result.failed() == ["fix_target"] and "misses 2" in result.checks[-2].detail


def test_auto_promotion_writes_learned_and_a_decision(tmp_path, library, re2):
    root = library.path
    user_bytes = (root / "library.yaml").read_bytes()
    gate = run_gate(proposal(), evidence(), library, re2)
    outcome = submit(root, proposal(), gate, policy="auto", library=library)
    assert outcome.action == "learned" and outcome.priority == 49  # below every approved message entry
    assert (root / "library.yaml").read_bytes() == user_bytes  # the human file is never written
    [learned] = yaml.safe_load((root / "learned.yaml").read_text())
    assert learned["status"] == "approved" and learned["evidence"]["gate"]["passed"] is True
    [record] = yaml.safe_load((root / "decisions.yaml").read_text())
    assert record["op"] == "approve" and record["by"] == "system:auto" and len(record["examples"]) == 3
    reloaded = load_library(root, re2)
    assert reloaded.entries["syn.cache_miss"].status == "approved" and reloaded.entries["syn.cache_miss"].owner == "learned"
    assert submit(root, proposal(), gate, policy="auto", library=reloaded).action == "rejected"  # id exists


def test_manual_promotion_waits_for_a_human(library, re2):
    root = library.path
    outcome = submit(root, proposal(), run_gate(proposal(), evidence(), library, re2), policy="manual", library=library)
    assert outcome.action == "proposed" and not (root / "learned.yaml").exists() and not (root / "decisions.yaml").exists()
    reloaded = load_library(root, re2)
    assert reloaded.entries["syn.cache_miss"].status == "proposed"
    assert "syn.cache_miss" not in {e.entry.id for e in reloaded.approved()}


def test_auto_never_promotes_other_kinds_or_failed_gates(library, re2):
    root = library.path
    noise = proposal(kind="noise")
    noise_gate = run_gate(noise, evidence(), library, re2)
    assert noise_gate.passed
    assert submit(root, noise, noise_gate, policy="auto", library=library).action == "proposed"
    bad = proposal(r"^cache (?P<a>\S+)\S+ ms$")
    failed = submit(root, bad, run_gate(bad, evidence(), library, re2), policy="auto", library=library)
    assert failed.action == "rejected" and "lint" in failed.reason
    fix_of_user = submit(
        root,
        proposal().model_copy(update={"id": "syn.fix"}),
        run_gate(proposal(), evidence(), library, re2),
        policy="auto",
        library=library,
        target_pattern_id="msg.dependency.connect_failed",
    )
    assert fix_of_user.action == "proposed" and "human-owned" in fix_of_user.reason


def test_the_configured_gate_reads_pattern_library_settings(library, re2):
    from su_multi_arm.core.config import PatternLibraryRef
    from su_multi_arm.learning.patterns.gate import run_configured_gate

    strict = PatternLibraryRef.model_validate({"path": str(library.path), "gate": {"min_support": 50}})
    assert run_configured_gate(proposal(), evidence(), library, re2, strict).failed() == ["min_support"]
    regex_engine = engine_or_skip("regex")
    relaxed = PatternLibraryRef.model_validate({"path": str(library.path), "engine": "regex", "gate": {"require_re2": False}})
    assert run_configured_gate(proposal(), evidence(), library, regex_engine, relaxed).passed
