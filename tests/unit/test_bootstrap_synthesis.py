"""L4a: deterministic Drain-style miner with bounded memory, regex synthesis, and bootstrap coverage on
synthetic fixtures without an LLM (addendum §3.6.1 rows 1–3, traceability ``test_bootstrap_synthesis``)."""

from __future__ import annotations

import pytest

from su_multi_arm.extraction.patterns.engine import validate_regex
from su_multi_arm.extraction.patterns.extractor import CompiledLibrary
from su_multi_arm.extraction.patterns.library import load_library
from su_multi_arm.learning.patterns import LogLine, MinerConfig, TemplateMiner, bootstrap, synthesise
from su_multi_arm.learning.patterns.gate import GateEvidence, run_gate
from su_multi_arm.learning.patterns.synthesis import template_regex
from tests.pattern_fixtures import CONNECT, FORMAT, MASKS, engine_or_skip, synthetic_lines, write_library


@pytest.fixture(params=["re2", "regex"])
def engine(request):
    return engine_or_skip(request.param)


def compiled(tmp_path, engine, patterns):
    return CompiledLibrary(load_library(write_library(tmp_path, patterns), engine), engine, 8192)


def test_miner_masks_and_merges_variable_tokens(engine):
    miner = TemplateMiner(engine, MASKS)
    for i, word in enumerate(["alpha", "bravo", "delta"]):
        miner.add(f"l{i}", f"job {word} finished with status {i * 7}", "svc", "info")
    miner.add("l9", "worker pool resized from 1 to 2 slots", "svc", "info")
    [job, pool] = miner.result()
    assert job.template() == "job <*> finished with status <num>" and job.size == 3
    assert pool.template() == "worker pool resized from <num> to <num> slots"
    assert [i for i, _ in job.examples] == ["l0", "l1", "l2"]


def test_miner_keys_by_service_and_level(engine):
    miner = TemplateMiner(engine, MASKS)
    miner.add("a", "disk full on node 3", "svc-a", "error")
    miner.add("b", "disk full on node 4", "svc-b", "error")
    miner.add("c", "disk full on node 5", "svc-a", "warn")
    assert len(miner.result()) == 3


def test_miner_memory_is_bounded_and_evictions_are_counted(engine):
    config = MinerConfig(max_clusters=5, max_examples=2)
    miner = TemplateMiner(engine, MASKS, config)
    for i in range(40):
        word = chr(97 + i // 26) + chr(97 + i % 26)
        miner.add(f"l{i}", f"ev{word} happened here", "svc", "info")  # 40 distinct (unmasked) first tokens
    assert len(miner.clusters) == 5 and miner.evictions == 35 and miner.stats()["clusters"] == 5
    assert all(len(c.examples) <= 2 for c in miner.result())
    miner.add("long", " ".join(["tok"] * 100), "svc", "info")
    miner.add("ctl", "bad \x01 line", "svc", "info")
    assert miner.skipped == 2


def test_miner_is_deterministic(engine):
    lines = synthetic_lines(300, seed=11)

    def mine():
        miner = TemplateMiner(engine, MASKS)
        for line_id, text, attrs in lines:
            miner.add(line_id, text, attrs["service"], None)
        return [(c.id, c.template(), c.size, c.examples) for c in miner.result()]

    assert mine() == mine()


def test_synthesised_regex_is_lint_clean_and_matches_its_cluster(engine):
    miner = TemplateMiner(engine, MASKS)
    for i in range(4):
        miner.add(f"l{i}", f"session {i:08x}-0000-0000-0000-00000000000{i} expired after {i + 10} seconds idle", "gw", "warn")
    [cluster] = miner.result()
    made = synthesise(cluster, MASKS, engine)
    assert made.entry is not None, made.reason
    entry = made.entry
    assert [i for i in validate_regex(entry.regex) if i.severity == "error"] == []
    assert entry.kind == "message" and entry.status == "proposed" and entry.id.startswith("syn.") and entry.signature.id == entry.id
    assert entry.applies_to == {"service": ["gw"], "level": ["warn"]} and entry.fields["num1"].type == "integer"
    assert entry.fields["uuid1"].type == "string" and len(entry.examples.positive) == 4
    assert synthesise(cluster, MASKS, engine).entry.id == entry.id  # deterministic id


def test_over_broad_templates_are_rejected_not_loosened(engine):
    miner = TemplateMiner(engine, MASKS)
    for i in range(3):
        miner.add(f"l{i}", f"{i} {i + 1}", "svc", "info")
    [cluster] = miner.result()
    made = synthesise(cluster, MASKS, engine)
    assert made.entry is None and "over-broad" in made.reason


def test_template_regex_escapes_literals():
    regex, origins = template_regex(["a.b(c)", "\x01*\x02", "x=\x01num\x02ms"], MASKS)
    assert regex.startswith("^ *a\\.b\\(c\\) +(?P<v1>\\S+) +x=(?P<num1>") and regex.endswith("ms *$")
    assert origins == {"v1": "*", "num1": "num"}


def test_bootstrap_coverage_without_an_llm(tmp_path, engine):
    library = compiled(tmp_path, engine, [FORMAT, CONNECT])
    lines = [LogLine(i, text, attrs) for i, text, attrs in synthetic_lines(700)]
    report = bootstrap(lines, library)
    assert report.before.coverage == pytest.approx(100 / 700)
    assert report.after.coverage >= 0.95 and report.rejected == []
    assert report.miner["evictions"] == 0 and report.proposals
    # every proposal is a gate-ready, lint-clean message pattern with its support recorded
    for entry in report.proposals:
        assert entry.kind == "message" and [i for i in validate_regex(entry.regex) if i.severity == "error"] == []
        assert len(report.support[entry.id]) >= 3
    assert bootstrap(lines, library).to_dict() == report.to_dict()  # deterministic


def test_bootstrap_proposals_pass_the_gate(tmp_path):
    re2 = engine_or_skip("re2")
    library = compiled(tmp_path, re2, [FORMAT, CONNECT])
    lines = [LogLine(i, text, attrs) for i, text, attrs in synthetic_lines(140)]
    report = bootstrap(lines, library)
    bodies = {}
    for line in lines:
        result = library.match_line(line.text, attributes=line.attributes)
        if result.format is not None:
            start, end = result.format.groups["body"]
            bodies[line.id] = result.text[start:end]
    entry = report.proposals[0]
    support = report.support[entry.id]
    cited = [i for i in support if bodies[i] in entry.examples.positive][: len(entry.examples.positive)]
    held_out = {"msg.dependency.connect_failed": [b for i, b in bodies.items() if "connect" in b or "refused" in b]}
    evidence = GateEvidence(lines={i: bodies[i] for i in support}, positive_ids=cited, held_out=held_out)
    result = run_gate(entry, evidence, library.library, re2)
    assert result.passed, result.summary()


def test_a_regex_over_the_entry_limit_is_skipped_with_its_reason_never_aborting(tmp_path, engine):
    """P19 (real-run fix): a template with many mask placeholders synthesises a regex longer than a
    library entry accepts (4,096 characters). The cluster is rejected as ``regex_too_long``; the
    bootstrap (``patterns mine``) still reports the other clusters."""
    from su_multi_arm.extraction.patterns.library import MAX_REGEX_CHARS

    library = compiled(tmp_path, engine, [])

    def uuids(i: int) -> str:
        return " ".join(f"{(i * 60 + k):08x}-0000-4000-8000-{k:012x}" for k in range(60))

    lines = [LogLine(f"u{i}", f"sync {uuids(i)} done", {"service": "svc"}) for i in range(3)]
    lines += [LogLine(f"w{i}", f"worker pool resized from {i} to {i + 1} slots", {"service": "svc"}) for i in range(3)]
    miner = TemplateMiner(engine, MASKS)
    for line in lines[:3]:
        miner.add(line.id, line.text, "svc", None)
    [cluster] = miner.result()
    made = synthesise(cluster, MASKS, engine)
    assert made.entry is None and made.reason.startswith("regex_too_long") and str(MAX_REGEX_CHARS) in made.reason
    report = bootstrap(lines, library)
    assert any(reason.startswith("regex_too_long") for _, reason in report.rejected)
    assert len(report.proposals) == 1 and "worker" in report.proposals[0].examples.positive[0]
