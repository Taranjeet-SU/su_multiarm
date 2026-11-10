"""Retrieval-parity features on the sqlite backend with the hash embedder (synthetic corpus, no
model download): the rerank stage in the engine, the child-record ``segment`` arm and imported
communities with grounded claims (``communities.strategy: imported`` + ``community.claim_hits``)."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3

import numpy as np
import pytest

from su_multi_arm import Document, Engine, SearchRequest
from su_multi_arm.communities.imported import CommunityImportError
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.ingestion.write import IngestError
from su_multi_arm.runtime.acl import Principal, StaticPolicy
from su_multi_arm.segments import SegmentIndex, SegmentIndexError, SegmentRecord, build_segment_index, embedder_recipe
from su_multi_arm.segments.build import build_from_config
from su_multi_arm.selection.rerank import Reranker, RerankError

TICKETS = [
    ("t1", "team_a", "Crawler stuck", "The crawler stopped at forty percent.", "Restarting the crawl worker fixed the stuck crawler."),
    ("t2", "team_a", "Login loop", "Users are sent back to the login page.", "Clearing the session cookie domain fixed the login loop."),
    ("t3", "team_b", "Slow search", "Search takes ten seconds for every query.", "Rebuilding the index shards fixed the slow search."),
    ("t4", "team_b", "Crawler timeout", "The crawler times out on large sitemaps.", "Raising the crawler timeout fixed large sitemaps."),
]


def documents(resolution_overrides=None):
    out = []
    for rid, team, title, description, resolution in TICKETS:
        resolution = (resolution_overrides or {}).get(rid, resolution)
        out.append(Document(id=rid, dataset="demo", source=team, title=title, fields={"description": description, "resolution": resolution}, metadata={"team": team}))
    return out


def base(tmp_path, **pipeline):
    arms = {"dense": {"backend": "local"}, "lexical": {"backend": "local"}}
    arms.update(pipeline.pop("arms", {}))
    data = {
        "schema_version": 2,
        "dataset": "demo",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "state")}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": {"fields": [{"name": "team", "type": "string", "filterable": True, "listable": True}]},
        "pipeline": {"arms": arms, **pipeline},
    }
    return data


def segment_rows():
    rows = []
    for rid, _team, _title, description, resolution in TICKETS:
        rows.append(SegmentRecord(id=f"{rid}:d", parent_id=rid, kind="description", text=description, field="description", seq=0))
        rows.append(SegmentRecord(id=f"{rid}:r", parent_id=rid, kind="resolution", text=resolution, field="resolution", seq=1))
    return rows


def write_rows(path, rows):
    path.write_text("\n".join(r.model_dump_json() for r in rows) + "\n")
    return path


@pytest.fixture
def seg_engine(tmp_path):
    seed = Engine(validate_config(base(tmp_path)))
    seed.ingest(documents())
    out = tmp_path / "segments"
    manifest = build_from_config(seed.config, write_rows(tmp_path / "segments.jsonl", segment_rows()), out, engine=seed)
    config = validate_config(base(tmp_path, arms={"segment": {"path": str(out), "lexical": True}}))
    return Engine(config), manifest, out


# --- segments --------------------------------------------------------------------------------


def test_segment_index_build_manifest_and_parent_hits(seg_engine):
    engine, manifest, _ = seg_engine
    assert manifest["segments"] == 8 and manifest["parents"] == 4 and manifest["anchored"] == 8
    assert manifest["kinds"] == {"description": 4, "resolution": 4}
    result = engine.search(SearchRequest(query="restarting the crawl worker"))
    hits = result.arm_hits["segment"]
    assert hits[0].document_id == "t1"
    assert len({h.document_id for h in hits}) == len(hits)  # one hit per parent
    best = hits[0].provenance[0]
    assert best["segment_id"] == "t1:r" and best["segment_kind"] == "resolution" and best["anchor"] == "verified"
    assert best["parent_id"] == "t1" and hits[0].text == best["text"]
    doc = engine.get_document("t1") if hasattr(engine, "get_document") else None
    if doc is not None:
        assert doc.fields[best["field"]][best["start"] : best["end"]] == best["text"]
    report = next(r for r in result.trace["reports"] if r.get("kind") == "segment")
    assert report["stale_anchors"] == 0 and report["parents_admitted"] >= 1


def test_segment_kinds_narrow_the_search(tmp_path, seg_engine):
    _engine, _manifest, out = seg_engine
    engine = Engine(validate_config(base(tmp_path, arms={"segment": {"path": str(out), "kinds": ["description"]}})))
    hits = engine.search(SearchRequest(query="crawler")).arm_hits["segment"]
    assert hits and all(p["segment_kind"] == "description" for h in hits for p in h.provenance)


def test_segment_scope_is_evaluated_on_the_parent(seg_engine):
    engine, _, _ = seg_engine
    filtered = engine.search(SearchRequest(query="crawler", filters={"team": "team_b"})).arm_hits["segment"]
    assert filtered and {h.document_id for h in filtered} <= {"t3", "t4"}
    who = Principal(id="u", tenant="team_a", scopes=["search"])
    acl_engine = Engine(engine.config, acl=StaticPolicy(field="team", attribute="tenant"))
    scoped = acl_engine.search(SearchRequest(query="crawler timeout sitemaps"), principal=who).arm_hits["segment"]
    assert scoped and {h.document_id for h in scoped} <= {"t1", "t2"}
    excluded = engine.search(SearchRequest(query="crawler", exclude_ids=["t1", "t4"])).arm_hits["segment"]
    assert {h.document_id for h in excluded}.isdisjoint({"t1", "t4"})


def test_stale_anchor_is_dropped_never_cited(tmp_path, seg_engine):
    engine, _, _ = seg_engine
    engine.ingest(documents({"t1": "A different resolution text."}), replace=True)
    result = engine.search(SearchRequest(query="restarting the crawl worker fixed the stuck crawler"))
    cited = [p["segment_id"] for h in result.arm_hits["segment"] for p in h.provenance]
    assert "t1:r" not in cited
    report = next(r for r in result.trace["reports"] if r.get("kind") == "segment")
    assert report["stale_anchors"] >= 1


def test_anchor_required_fails_and_optional_records_unanchored(tmp_path):
    seed = Engine(validate_config(base(tmp_path)))
    seed.ingest(documents())
    rows = [*segment_rows(), SegmentRecord(id="t1:x", parent_id="t1", kind="comment", text="Not present in the record.", field="description")]
    with pytest.raises(SegmentIndexError, match="not found verbatim"):
        build_from_config(seed.config, write_rows(tmp_path / "a.jsonl", rows), tmp_path / "a", engine=seed)
    manifest = build_from_config(seed.config, write_rows(tmp_path / "b.jsonl", rows), tmp_path / "b", anchor="optional", engine=seed)
    assert manifest["unanchored"] == 1 and manifest["anchored"] == 8
    with pytest.raises(SegmentIndexError, match="not in generation"):
        build_from_config(seed.config, write_rows(tmp_path / "c.jsonl", [SegmentRecord(id="z", parent_id="nope", kind="k", text="x")]), tmp_path / "c", engine=seed)
    with pytest.raises(SegmentIndexError, match="already holds"):
        build_from_config(seed.config, write_rows(tmp_path / "b.jsonl", rows), tmp_path / "b", anchor="optional", engine=seed)


def test_tampered_or_incompatible_index_is_refused(tmp_path, seg_engine):
    engine, _, out = seg_engine
    copy = tmp_path / "copy"
    shutil.copytree(out, copy)
    vectors = np.load(copy / "vectors.npy")
    np.save(copy / "vectors.npy", vectors * 0.5)
    with pytest.raises(SegmentIndexError, match="digest"):
        SegmentIndex(copy)
    index = SegmentIndex(out)
    with pytest.raises(SegmentIndexError, match="dataset"):
        index.check("other", embedder_recipe(engine.embedder))
    with pytest.raises(SegmentIndexError, match="embedded with"):
        index.check("demo", {**embedder_recipe(engine.embedder), "dimensions": 999})
    with pytest.raises(SegmentIndexError, match="no sealed"):
        SegmentIndex(tmp_path / "missing")


def test_missing_index_is_a_validation_error(tmp_path):
    with pytest.raises(ConfigError, match="segment"):
        Engine(validate_config(base(tmp_path, arms={"segment": {"path": str(tmp_path / "none")}})))


def test_duplicate_segment_ids_are_refused(tmp_path, seg_engine):
    engine, _, _ = seg_engine
    rows = segment_rows()
    with pytest.raises(SegmentIndexError, match="duplicate"):
        build_segment_index([*rows, rows[0]], tmp_path / "dup", embedder=engine.embedder, parents={}, dataset="demo", generation="g")


# --- rerank in the engine ---------------------------------------------------------------------


class WordScorer:
    def __init__(self, word, fail=False):
        self.word, self.fail = word, fail

    def load(self):
        return {"device": "cpu", "dtype": "float32"}

    def score(self, pairs):
        if self.fail:
            raise RuntimeError("model crashed")
        return [float(p.lower().count(self.word)) for _q, p in pairs]


def rerank_engine(tmp_path, **reranking):
    data = base(tmp_path, selection={"rerank": True, "reranking": reranking})
    data["models"] = {"roles": {"reranker": {"model": "fake/cross-encoder"}}}
    engine = Engine(validate_config(data))
    engine.ingest(documents())
    return engine


def test_engine_rerank_reorders_and_traces(tmp_path):
    engine = rerank_engine(tmp_path, top_k_out=2)
    engine._reranker = Reranker(WordScorer("login"), model="fake")
    result = engine.search(SearchRequest(query="crawler login search"))
    assert result.hits[0].document_id == "t2" and len(result.hits) == 2
    detail = result.trace["selection"]["rerank_detail"]
    assert detail["status"] == "reranked" and detail["dropped"] >= 1 and result.trace["selection"]["rerank"] is True


def test_engine_rerank_failure_policy(tmp_path):
    engine = rerank_engine(tmp_path, on_error="fusion_order")
    engine._reranker = Reranker(WordScorer("login", fail=True), model="fake")
    result = engine.search(SearchRequest(query="crawler login search"))
    assert result.hits and result.trace["selection"]["rerank_detail"]["status"] == "failed_open"
    strict = rerank_engine(tmp_path / "strict")
    strict._reranker = Reranker(WordScorer("login", fail=True), model="fake")
    with pytest.raises(RerankError):
        strict.search(SearchRequest(query="crawler login search"))


# --- imported communities -----------------------------------------------------------------------


def community_file(tmp_path, rows):
    path = tmp_path / "communities.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


COMMUNITIES = [
    {
        "id": "crawl",
        "label": "Crawler problems",
        "members": ["t1", "t4", "ghost"],
        "summary": "Crawlers fail a lot and the product team should fix them.",
        "claims": [
            {"text": "Crawler issues were fixed by restarting or raising timeouts.", "evidence": [{"document_id": "t1", "quote": "Restarting the crawl worker fixed the stuck crawler."}, {"document_id": "t4", "quote": "Raising the crawler timeout"}]},
            {"text": "An invented claim.", "evidence": [{"document_id": "t1", "quote": "this text does not exist"}]},
            {"text": "A claim citing a non-member.", "evidence": [{"document_id": "t2", "quote": "Clearing the session cookie"}]},
        ],
    },
    {"id": "auth", "label": "Login loop", "members": ["t2"], "claims": []},
    {"id": "empty", "label": "Nobody", "members": ["ghost"]},
]


def community_engine(tmp_path, rows=COMMUNITIES, acl=False, **imported):
    path, digest = community_file(tmp_path, rows)
    data = base(tmp_path, arms={"community": {"backend": "local", "claim_hits": True}})
    data["communities"] = {"strategy": "imported", "imported": {"path": str(path), "sha256": digest, **imported}}
    return Engine(validate_config(data), **({"acl": StaticPolicy(field="team", attribute="tenant")} if acl else {}))


def test_imported_communities_keep_only_grounded_claims(tmp_path):
    engine = community_engine(tmp_path, extractive_sentences=2)
    manifest = engine.ingest(documents())
    report = manifest["community_import"]
    assert report["communities"] == 2 and report["dropped_no_members"] == 1 and report["absent_members"] == 2
    assert report["claims_in"] == 3 and report["claims_grounded"] == 1 and report["claims_dropped"] == 2
    assert report["summary_refused"] == 1 and report["extractive_communities"] == 1
    graph = engine.backends["local"].graph
    stored = {c.id: c for c in graph.communities(engine.write_path.current()[0])}
    crawl = stored["crawl"]
    assert crawl.document_ids == ["t1", "t4"] and crawl.report["status"] == "complete_input" and crawl.report["origin"] == "imported"
    assert [c["text"] for c in crawl.report["claims"]] == ["Crawler issues were fixed by restarting or raising timeouts."]
    assert all(ref["chunk_id"] for c in crawl.report["claims"] for ref in c["evidence"])
    assert "Crawlers fail a lot" not in json.dumps(crawl.report)
    auth = stored["auth"]
    assert auth.report["origin"] == "extractive" and auth.report["claims"]
    for claim in auth.report["claims"]:
        assert claim["text"] == claim["evidence"][0]["quote"] and claim["evidence"][0]["document_id"] == "t2"


def test_community_claim_hits_carry_lineage_and_respect_acl(tmp_path):
    engine = community_engine(tmp_path)
    engine.ingest(documents())
    result = engine.search(SearchRequest(query="crawler restarting timeouts fixed"))
    hits = result.arm_hits["community"]
    assert {h.document_id for h in hits} == {"t1", "t4"}
    provenance = hits[0].provenance[0]
    assert provenance["community_id"] == "crawl" and provenance["kind"] == "community_claim" and provenance["member_count"] == 2
    assert provenance["report_origin"] == "imported"
    who = Principal(id="u", tenant="team_a", scopes=["search"])  # t4 (team_b) is not eligible
    engine = Engine(engine.config, acl=StaticPolicy(field="team", attribute="tenant"))
    scoped = engine.search(SearchRequest(query="crawler restarting timeouts fixed"), principal=who)
    assert not scoped.arm_hits.get("community")  # the claim cites an ineligible member: never shown
    reports = [r for r in scoped.trace["reports"] if r.get("community_id") == "crawl"]
    assert all(r["label"] == "Scoped community" and not r["claims"] for r in reports)


def test_claim_hits_off_by_default(tmp_path):
    path, digest = community_file(tmp_path, COMMUNITIES)
    data = base(tmp_path, arms={"community": {"backend": "local"}})
    data["communities"] = {"strategy": "imported", "imported": {"path": str(path), "sha256": digest}}
    engine = Engine(validate_config(data))
    engine.ingest(documents())
    assert not engine.search(SearchRequest(query="crawler restarting timeouts fixed")).arm_hits.get("community")


def test_import_digest_fail_mode_and_config_coherence(tmp_path):
    engine = community_engine(tmp_path, on_ungrounded="fail")
    with pytest.raises(IngestError, match="not grounded"):
        engine.ingest(documents())
    path, _ = community_file(tmp_path, COMMUNITIES)
    data = base(tmp_path / "y")
    data["communities"] = {"strategy": "imported", "imported": {"path": str(path), "sha256": "0" * 64}}
    with pytest.raises(IngestError, match="sha256"):
        Engine(validate_config(data)).ingest(documents())
    with pytest.raises(CommunityImportError):
        from su_multi_arm.communities.imported import imported_communities

        imported_communities(validate_config(data).communities.imported, {"t1"}, [])
    with pytest.raises(ValueError, match="imported"):
        validate_config({**base(tmp_path), "communities": {"strategy": "imported"}})
    with pytest.raises(ValueError, match="imported"):
        validate_config({**base(tmp_path), "communities": {"imported": {"path": str(path), "sha256": "0" * 64}}})


# --- ingestion.attach -----------------------------------------------------------------------------


def kg_file(directory, rows, duplicate_view=False):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "kg.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE insights (ticket INTEGER PRIMARY KEY, outcome TEXT, request_type TEXT, secret TEXT)")
    db.executemany("INSERT INTO insights VALUES (?,?,?,?)", rows)
    db.execute("CREATE VIEW insight_view AS SELECT ticket, outcome FROM insights")
    if duplicate_view:
        db.execute("CREATE VIEW dup AS SELECT ticket, outcome FROM insights UNION ALL SELECT ticket, outcome FROM insights")
    db.commit()
    db.close()
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def attach_config(tmp_path, path, digest, **extra):
    data = base(tmp_path)
    data["metadata_schema"]["fields"] += [
        {"name": "outcome", "type": "string", "filterable": True, "listable": True, "groupable": True, "description": "Outcome class."},
        {"name": "request_type", "type": "string", "filterable": True},
    ]
    source = {"path": str(path), "sha256": digest, "table": "insights", "key_column": "ticket", "document_id": "t{key}", "columns": {"outcome": "outcome", "request_type": "request_type"}, **extra}
    data["ingestion"] = {"attach": [source]}
    return data


def test_attach_joins_declared_columns_onto_metadata(tmp_path):
    path, digest = kg_file(tmp_path / "kg", [(1, "fixed", "break_fix", "x"), (3, "answered", None, "y"), (9, "orphan", "z", "w")])
    engine = Engine(validate_config(attach_config(tmp_path, path, digest)))
    manifest = engine.ingest(documents())
    report = manifest["attach"][0]
    assert (report["rows"], report["matched"], report["unmatched"], report["orphan_rows"]) == (3, 2, 2, 1)
    corpus = engine.write_path._load_corpus(engine.write_path.current()[0])
    assert corpus["t1"].metadata["outcome"] == "fixed" and corpus["t1"].metadata["request_type"] == "break_fix"
    assert "request_type" not in corpus["t3"].metadata  # NULL is not attached
    assert "outcome" not in corpus["t2"].metadata and "secret" not in json.dumps(corpus["t1"].metadata)
    hits = engine.search(SearchRequest(query="crawler", filters={"outcome": "fixed"})).hits
    assert [h.document_id for h in hits] == ["t1"]


def test_attach_is_fail_closed(tmp_path):
    from su_multi_arm.ingestion.attach import AttachError

    path, digest = kg_file(tmp_path / "kg", [(1, "fixed", "a", "x")], duplicate_view=True)
    data = attach_config(tmp_path, path, digest)
    data["ingestion"]["attach"][0]["columns"]["secret"] = "undeclared_field"
    with pytest.raises(ConfigError, match="metadata_schema"):
        validate_config(data)
    with pytest.raises((AttachError, IngestError), match="sha256"):
        Engine(validate_config(attach_config(tmp_path / "a", path, "0" * 64))).ingest(documents())
    clash = [documents()[0].model_copy(update={"metadata": {"team": "team_a", "outcome": "mine"}})]
    with pytest.raises((AttachError, IngestError), match="already has metadata"):
        Engine(validate_config(attach_config(tmp_path / "b", path, digest))).ingest(clash)
    kept = Engine(validate_config(attach_config(tmp_path / "c", path, digest, on_conflict="keep_document"))).ingest(clash)
    assert kept["attach"][0]["conflicts_kept_document"] == 1
    with pytest.raises(ValueError):
        validate_config(attach_config(tmp_path, path, digest, table="insights; DROP TABLE x"))
    view = attach_config(tmp_path / "d", path, digest, table="insight_view", columns={"outcome": "outcome"})
    assert Engine(validate_config(view)).ingest(documents())["attach"][0]["matched"] == 1
    dup = attach_config(tmp_path / "f", path, digest, table="dup", columns={"outcome": "outcome"})
    with pytest.raises((AttachError, IngestError), match="not unique"):
        Engine(validate_config(dup)).ingest(documents())
