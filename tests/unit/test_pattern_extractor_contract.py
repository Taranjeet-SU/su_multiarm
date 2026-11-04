"""The ``pattern`` extractor as a first-class extractor (addendum §3.4.2–3.4.4): the §3.3 example end to
end through ``ExtractionOutput`` validation, span invariant, fact vs co_occurrence, header attribute
links, residue composition, determinism, truncation, recipe/fingerprint isolation and the reuse salt."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from su_multi_arm.core.component import Dependencies, Environment
from su_multi_arm.core.config import ExtractionConfig
from su_multi_arm.core.types import Document, ExtractionOutput
from su_multi_arm.extraction.base import BuiltinExtractor, ExtractionFailure, ExtractionInput
from su_multi_arm.extraction.chunking import chunk_document
from su_multi_arm.extraction.patterns.engine import PatternTimeout, re2_available
from su_multi_arm.extraction.patterns.library import PatternLibraryError
from tests.pattern_fixtures import CONNECT, FORMAT, engine_or_skip, write_library, write_system

LINE_OK = 'ts=2026-11-04T10:00:00.000Z lvl=error comp=net.client msg="db-client failed to connect redis after 30 ms"'
LINE_UNPARSED = 'ts=2026-11-04T10:00:01.000Z lvl=info comp=sched.loop msg="scheduler tick for redis"'
LINE_PLAIN = "queue-client refused by kafka"


@pytest.fixture(params=["re2", "regex"])
def kind(request):
    engine_or_skip(request.param)
    return request.param


def extractor(root, kind, extractors=("pattern",), **extra) -> BuiltinExtractor:
    library = {"path": str(root), "engine": kind, **extra.pop("library", {})}
    cfg = ExtractionConfig.model_validate({"extractors": list(extractors), "pattern_library": library, **extra})
    return BuiltinExtractor(cfg, Dependencies(providers={}))


def run(ex: BuiltinExtractor, text: str, field: str = "message", metadata=None, kind_: str = "document"):
    doc = Document(id="d1", kind=kind_, fields={field: text}, metadata=metadata or {"service": "query-gateway"})
    chunks = [c for c in chunk_document(doc, 4000, 100, []) if c.field == field]
    assert len(chunks) == 1
    return ex.extract(ExtractionInput(chunk=chunks[0], document=doc, chunks=chunks)), chunks[0]


def by_name(output: ExtractionOutput) -> dict[str, str]:
    return {e.name: e.type for e in output.entities}


def test_example_end_to_end_through_extraction_output(tmp_path, kind):
    ex = extractor(write_library(tmp_path, [FORMAT, CONNECT]), kind)
    result, chunk = run(ex, LINE_OK + "\n" + LINE_UNPARSED)
    output = ExtractionOutput.model_validate(result.output.model_dump())  # re-validated: endpoints contained
    assert by_name(output) == {"net.client": "component", "db-client": "component", "redis": "dependency", "sched.loop": "component"}
    [relation] = output.relations
    assert relation.predicate == "fails_to_connect" and relation.edge_kind == "fact" and relation.support == "trigger_in_span"
    assert relation.span.text == "db-client failed to connect redis" and relation.provenance.extractor == "pattern:msg.dependency.connect_failed"
    for span in [relation.span, *(e.span for e in output.entities)]:
        assert span.text == chunk.text[span.start - chunk.start : span.end - chunk.start] and span.match_level == "exact"
    assert result.routing["pattern"] == {"lines": 2, "parsed": 1, "format_only": 1, "truncated": 0}


def test_level_selector_reads_the_format_group(tmp_path, kind):
    ex = extractor(write_library(tmp_path, [FORMAT, CONNECT]), kind)
    info = LINE_OK.replace("lvl=error", "lvl=info")
    result, _ = run(ex, info)
    assert result.output.relations == [] and result.routing["pattern"]["format_only"] == 1


def test_message_runs_on_the_whole_line_without_a_format_match(tmp_path, kind):
    ex = extractor(write_library(tmp_path, [CONNECT]), kind)
    result, _ = run(ex, LINE_PLAIN, metadata={"service": "doc-fetcher", "level": "warn"})
    assert by_name(result.output) == {"queue-client": "component", "kafka": "dependency"}
    assert result.output.relations[0].edge_kind == "fact"


TALK = {
    "id": "msg.talk",
    "kind": "message",
    "status": "approved",
    "regex": r"^(?P<a>[a-z]+) (?P<neg>never )?(?P<t>talks to) (?P<b>[a-z]+)(?: via (?P<via>[a-z]+))?$",
    "signature": {"id": "talk"},
    "entities": [{"group": "a", "type": "node"}, {"group": "b", "type": "node"}, {"group": "via", "type": "node"}],
    "relations": [
        {"subject": "a", "predicate": "talks_to", "object": "b", "trigger_group": "t"},
        {"subject": "a", "predicate": "routes_through", "object": "via"},
        {"subject": "b", "predicate": "talks_to", "object": "a"},
    ],
    "examples": {"positive": ["alpha talks to beta"]},
}


def test_fact_versus_co_occurrence(tmp_path, kind):
    ex = extractor(write_library(tmp_path, [TALK]), kind)
    result, _ = run(ex, "alpha talks to beta")
    kinds = {(r.predicate, r.edge_kind) for r in result.output.relations}
    # trigger group matched -> fact; no trigger group: predicate support decides ("talks to" in span)
    assert kinds == {("talks_to", "fact")} and len(result.output.relations) == 2
    negated, _ = run(ex, "alpha never talks to beta")
    assert {(r.predicate, r.edge_kind, r.support) for r in negated.output.relations} == {("talks_to", "co_occurrence", "unchecked")}


def test_optional_group_absent_means_no_entity_and_no_relation(tmp_path, kind):
    ex = extractor(write_library(tmp_path, [TALK]), kind)
    result, _ = run(ex, "alpha talks to beta")
    assert "routes_through" not in {r.predicate for r in result.output.relations}
    routed, _ = run(ex, "alpha talks to beta via gamma")
    [route] = [r for r in routed.output.relations if r.predicate == "routes_through"]
    assert route.edge_kind == "co_occurrence" and route.span.text == "alpha talks to beta via gamma"


def test_header_attribute_links_are_native_links(tmp_path, kind):
    ex = extractor(write_library(tmp_path, [FORMAT, CONNECT]), kind)
    metadata = {"service": "query-gateway", "signature_id": "dependency.connect_failed"}
    header = "signature dependency.connect_failed on service query-gateway"
    result, chunk = run(ex, header, field="header", metadata=metadata, kind_="signature")
    [link] = result.output.relations
    assert link.edge_kind == "native_link" and link.support == "unchecked" and link.predicate == "emits"
    assert link.span.text == chunk.text[link.span.start : link.span.end] and link.span.field == "header"
    assert by_name(result.output) == {"query-gateway": "service", "dependency.connect_failed": "signature"}
    missing, _ = run(ex, "signature dependency.connect_failed", field="header", metadata=metadata, kind_="signature")
    assert missing.output.relations == []
    # header chunks are never line-matched, and other fields never produce attribute links
    body, _ = run(ex, LINE_OK, metadata=metadata)
    assert all(r.edge_kind != "native_link" for r in body.output.relations)


def test_residue_composition_runs_later_extractors_on_unparsed_lines(tmp_path, kind):
    root = write_library(tmp_path, [FORMAT, CONNECT])
    ex = extractor(root, kind, extractors=("pattern", "gazetteer"), gazetteer={"store": ["redis"]})
    result, _ = run(ex, LINE_OK + "\n" + LINE_UNPARSED)
    stores = [e for e in result.output.entities if e.type == "store"]
    assert len(stores) == 1 and stores[0].span.start > len(LINE_OK)  # only the unparsed line's mention
    assert result.routing["residue_only"] is True and result.routing["residue_to"] == ["gazetteer"]
    parsed_only, _ = run(ex, LINE_OK)
    assert parsed_only.routing["residue_to"] == [] and not [e for e in parsed_only.output.entities if e.type == "store"]
    # not first: every extractor sees the whole chunk
    ex2 = extractor(root, kind, extractors=("gazetteer", "pattern"), gazetteer={"store": ["redis"]})
    both, _ = run(ex2, LINE_OK + "\n" + LINE_UNPARSED)
    [first_store] = [e for e in both.output.entities if e.type == "store"]  # the gazetteer keeps a name's first mention
    assert first_store.span.start < len(LINE_OK) and "residue_only" not in both.routing


def test_residue_extractor_cannot_reach_into_a_blanked_line(tmp_path, kind):
    root = write_library(tmp_path, [FORMAT, CONNECT])
    ex = extractor(root, kind, extractors=("pattern", "regex"), patterns={"stall": r"stalled\s+"})
    with pytest.raises(ExtractionFailure, match="grounding"):
        run(ex, "queue stalled\n" + LINE_OK)


def test_long_lines_are_truncated_before_matching(tmp_path, kind):
    ex = extractor(write_library(tmp_path, [CONNECT]), kind, library={"max_line_bytes": 256})
    long_line = LINE_PLAIN + " " + "x" * 400
    result, _ = run(ex, long_line, metadata={"level": "error"})
    assert result.routing["pattern"]["truncated"] == 1 and result.output.relations[0].edge_kind == "fact"
    later = "x" * 300 + " " + LINE_PLAIN  # the match lies beyond the cut: never seen
    cut, _ = run(ex, later, metadata={"level": "error"})
    assert cut.output.entities == [] and cut.routing["pattern"]["unparsed"] == 1


def test_extraction_is_deterministic(tmp_path, kind):
    root = write_library(tmp_path, [FORMAT, CONNECT, TALK])
    text = "\n".join([LINE_OK, LINE_UNPARSED, "alpha talks to beta via gamma", LINE_OK.replace("redis", "kafka")])
    first, _ = run(extractor(root, kind), text)
    second, _ = run(extractor(root, kind), text)
    assert first.output.model_dump() == second.output.model_dump()


def test_timeout_lines_stay_in_the_residue(tmp_path, monkeypatch):
    engine_or_skip("regex")
    ex = extractor(write_library(tmp_path, [FORMAT, CONNECT]), "regex", extractors=("pattern", "gazetteer"), gazetteer={"store": ["redis"]})
    from su_multi_arm.extraction.patterns import engine as engine_module

    ex._pattern.load()  # examples are checked at load; the timeout is simulated for extraction only

    def slow(self, compiled, text):
        raise PatternTimeout("simulated")

    monkeypatch.setattr(engine_module.RegexEngine, "search", slow)
    result, _ = run(ex, LINE_OK)
    assert result.routing["pattern"]["timeout"] == 1 and [e.type for e in result.output.entities] == ["store"]
    assert ex._pattern.provenance()["engine_deterministic"] is False


def test_set_prefilter_limits_full_searches(tmp_path, monkeypatch):
    if not re2_available():
        pytest.skip("google-re2 not installed")
    others = [dict(copy.deepcopy(TALK), id=f"msg.other{i}", signature={"id": f"other{i}"}, regex=TALK["regex"].replace("talks to", f"pings{i}"), examples={"positive": [f"alpha pings{i} beta"]}) for i in range(5)]
    for other in others:
        other["relations"] = [{"subject": "a", "predicate": "pings", "object": "b"}]
    ex = extractor(write_library(tmp_path, [CONNECT, *others]), "re2")
    from su_multi_arm.extraction.patterns import engine as engine_module

    ex._pattern.load()
    calls = []
    original = engine_module.Re2Engine.search
    monkeypatch.setattr(engine_module.Re2Engine, "search", lambda self, c, t: calls.append(t) or original(self, c, t))
    run(ex, LINE_PLAIN, metadata={"level": "error"})
    assert len(calls) == 1  # only the candidate the Set selected was searched


def test_config_couples_extractor_and_library(tmp_path):
    with pytest.raises(ValidationError, match="set together"):
        ExtractionConfig.model_validate({"extractors": ["pattern"]})
    with pytest.raises(ValidationError, match="set together"):
        ExtractionConfig.model_validate({"extractors": ["gazetteer"], "pattern_library": {"path": str(tmp_path)}})
    cfg = ExtractionConfig.model_validate({"extractors": ["pattern"], "pattern_library": {"path": str(tmp_path)}})
    assert cfg.pattern_library.promotion == "auto" and cfg.pattern_library.engine == "re2"


def test_recipe_excludes_library_contents_and_policy(tmp_path, kind):
    from su_multi_arm.core.component import recipe_fingerprint

    # unset: the recipe is the pre-2.0.0a3 dump (golden fingerprints in test_index_fingerprint_golden)
    plain = BuiltinExtractor(ExtractionConfig(), Dependencies(providers={}))
    data = {k: v for k, v in ExtractionConfig().model_dump(mode="json").items() if k != "pattern_library"}
    data.update(ontology=None, prompt_sha=plain.prompt_sha(), model=None)
    assert plain.recipe_fingerprint() == recipe_fingerprint("builtin", data)
    root = write_library(tmp_path, [FORMAT, CONNECT])
    base = extractor(root, kind).recipe_fingerprint()
    write_system(root, "proposed.yaml", [dict(copy.deepcopy(TALK), status="proposed")])
    write_system(root, "decisions.yaml", [{"op": "approve", "id": "msg.talk", "by": "user:a", "at": "2026-11-04T00:00:00Z"}])
    assert extractor(root, kind).recipe_fingerprint() == base
    assert extractor(root, kind, library={"promotion": "manual", "min_literal_chars": 9, "regex_timeout_ms": 5}).recipe_fingerprint() == base
    assert extractor(root, kind, library={"max_line_bytes": 4096}).recipe_fingerprint() != base
    assert extractor(tmp_path / "elsewhere", kind).recipe_fingerprint() == base  # path-independent


def test_reuse_salt_follows_the_effective_library(tmp_path, kind):
    assert BuiltinExtractor(ExtractionConfig(), Dependencies(providers={})).reuse_salt() == ""
    root = write_library(tmp_path, [FORMAT, CONNECT])
    write_system(root, "proposed.yaml", [dict(copy.deepcopy(TALK), status="proposed")])
    first = extractor(root, kind).reuse_salt()
    assert len(first) == 64 and extractor(root, kind).reuse_salt() == first
    write_system(root, "decisions.yaml", [{"op": "approve", "id": "msg.talk", "by": "user:a", "at": "2026-11-04T00:00:00Z"}])
    approved = extractor(root, kind).reuse_salt()
    write_system(
        root,
        "decisions.yaml",
        [{"op": "approve", "id": "msg.talk", "by": "user:a", "at": "2026-11-04T00:00:00Z"}, {"op": "deprecate", "id": "msg.talk", "by": "user:a", "at": "2026-11-04T01:00:00Z"}],
    )
    deprecated = extractor(root, kind).reuse_salt()
    assert len({first, approved, deprecated}) == 3


def test_library_files_are_read_lazily_and_validate_is_static(tmp_path, kind):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "library.yaml").write_text("library: broken\nformat_version: 1\npatterns: [{id: x, kind: message}]\n")
    ex = extractor(root, kind)
    ex.recipe_fingerprint()
    env = Environment(env={}, base_path=tmp_path)
    assert [d for d in ex.validate(env) if d.severity == "error"] == []  # no file content read
    probe = [d for d in ex.validate(env, probe=True) if d.severity == "error"]
    assert probe and probe[0].code == "pattern_library_lint"
    with pytest.raises(PatternLibraryError):
        ex.prepare_build()
    missing = extractor(tmp_path / "absent", kind)
    assert {d.code for d in missing.validate(env)} >= {"pattern_library_missing"}


def test_validate_reports_a_missing_engine(tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "re2", None)
    ex = extractor(write_library(tmp_path, [FORMAT]), "re2")
    codes = {d.code for d in ex.validate(Environment(env={}, base_path=tmp_path)) if d.severity == "error"}
    assert codes == {"pattern_engine_unavailable"}


def test_cli_patterns_lint_coverage_and_mine(tmp_path, kind, monkeypatch, capsys):
    import json

    from tests.pattern_fixtures import synthetic_lines
    from tests.unit.test_config_cli_a2 import run_main

    root = write_library(tmp_path / "patterns", [FORMAT, CONNECT])
    config = tmp_path / "config.yaml"
    config.write_text(
        "schema_version: 2\ndataset: logs\nbackends: {local: {kind: memory}}\n"
        "storage: {catalog: local, records: local, ledger: local, sessions: local, cache: local, communities: local}\n"
        "pipeline: {arms: {lexical: {backend: local}}}\n"
        f"extraction: {{extractors: [pattern], pattern_library: {{path: patterns, engine: {kind}}}}}\n"
    )
    (tmp_path / "lines.txt").write_text("\n".join(text for _, text, _ in synthetic_lines(70)) + "\n")
    before = sorted(p.name for p in root.iterdir())
    code, out, _ = run_main(monkeypatch, capsys, "--config", str(config), "patterns", "lint")
    lint = json.loads(out)
    assert code == 0 and lint["approved"] == ["fmt.kv_v1", "msg.dependency.connect_failed"] and lint["provenance"]["engine"] == kind
    code, out, _ = run_main(monkeypatch, capsys, "--config", str(config), "patterns", "coverage", "--input", str(tmp_path / "lines.txt"))
    assert code == 0 and json.loads(out)["by_status"] == {"format_only": 60, "parsed": 10}
    code, out, _ = run_main(monkeypatch, capsys, "--config", str(config), "patterns", "mine", "--input", str(tmp_path / "lines.txt"))
    mined = json.loads(out)
    assert code == 0 and mined["after"]["coverage"] == 1.0 and all(p["status"] == "proposed" for p in mined["proposals"])
    assert sorted(p.name for p in root.iterdir()) == before  # read-only
