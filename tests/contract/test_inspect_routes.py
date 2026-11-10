"""Read-only inspect routes (addendum A2 §4.3, slices S1a/S1b) on a generation built from the
synthetic logs fixtures: ``/v2/info`` from the manifest, run reports, the spend ledger (absent and
present), the generation's library snapshot, a bounded graph neighbourhood whose edges all come
from the store, edge provenance spans, communities reported as unavailable when none exist, the
``inspect`` scope and the single-tenant rule (B4). Synthetic data; no model calls."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from su_multi_arm.core.config import validate_config
from su_multi_arm.engine import Engine
from su_multi_arm.learning.patterns.spend import DaySpendLedger
from su_multi_arm.runtime.acl import Principal, StaticPolicy
from su_multi_arm.serving.http import create_app
from su_multi_arm.core.types import Entity, Occurrence, Path as GraphPath, PathEdge, Provenance, Span
from su_multi_arm.serving.inspect import NeighbourhoodRequest, edge_id, graph_edge, graph_neighbourhood, parse_edge_id

from logs_support import Clock, cluster_for, config_data, days, pipeline

WINDOW = days(date(2026, 1, 18), 2)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("inspect")
    cluster = cluster_for(WINDOW)
    clock = Clock(WINDOW[0])
    for day in WINDOW:
        clock.set_day(day)
        pipe, _cfg, _backend = pipeline(tmp, cluster, now=clock)
        pipe.run_daily(day)
    return tmp


@pytest.fixture
def client(built):
    return TestClient(create_app(engine=Engine(validate_config(config_data(built)))))


def test_info_reports_the_active_generation_window_and_provenance(client):
    info = client.get("/v2/info")
    assert info.status_code == 200 and info.headers["x-content-type-options"] == "nosniff"
    body = info.json()
    assert body["dataset"] == "logs" and body["logs"] is True and body["source"] == "synthetic"
    assert body["window"] == ["2026-01-10", "2026-01-19"] and body["as_of"] == "2026-01-19"
    assert body["date_basis"] == "index_date" and body["timezone"] == "UTC"
    assert [p["key"] for p in body["partitions"]] == ["2026-01-18", "2026-01-19"]
    assert body["served_days"] == ["2026-01-18", "2026-01-19"] and len(body["library_identities"]) == 1
    assert body["archived_days"] is None  # not recorded by this manifest: reported as unknown, not []
    assert body["pattern_provenance"]["library_identity"] and body["counts"]["documents"] > 0


def test_runs_are_content_free_summaries_newest_first(client):
    runs = client.get("/v2/runs", params={"limit": 5}).json()
    assert runs["available"] is True
    assert [r["day"] for r in runs["runs"]] == ["2026-01-19", "2026-01-18"] and all(r["ok"] for r in runs["runs"])
    partition = runs["runs"][0]["steps"]["partition"]
    assert partition["status"] == "sealed" and partition["counts"]["parsed"] > 0
    assert client.get("/v2/runs", params={"limit": 32}).status_code == 422


def test_spend_is_unavailable_without_a_ledger_and_read_only_with_one(built, client):
    absent = client.get("/v2/spend").json()
    assert absent["available"] is False and absent["rows"] == []
    today = datetime.now(timezone.utc)
    ledger = DaySpendLedger(built / "patterns" / ".spend_ledger.sqlite", max_calls=50, max_usd=1.0, clock=lambda: today)
    ledger.commit(ledger.reserve(0.25), 0.2)
    present = client.get("/v2/spend", params={"days": 3}).json()
    assert present["available"] is True
    assert [(r["scope"], r["calls"], r["committed_usd"], r["reserved_usd"]) for r in present["rows"]] == [("learning.patterns", 1, 0.2, 0.0)]
    assert client.get("/v2/spend", params={"days": 32}).status_code == 422
    (built / "patterns" / ".spend_ledger.sqlite").unlink()


def test_patterns_view_reads_the_generation_snapshot_not_working_files(built, client):
    view = client.get("/v2/patterns/view").json()
    assert view["available"] is True and view["snapshot_key"].startswith("pattern_library:")
    ids = {e["id"] for e in view["entries"]}
    assert {"fmt.bracket_node", "msg.dependency.connect_failed", "noise.healthz"} <= ids
    entry = next(e for e in view["entries"] if e["id"] == "msg.dependency.connect_failed")
    assert entry["signature"] == "dependency.connect_failed" and entry["regex"]
    assert entry["owner"] is None  # the snapshot records no author for this entry: not inferred
    assert view["proposals"] is None and view["proposals_reason"]
    (built / "patterns" / "library.yaml").write_text("not: a library")  # working file edits do not leak in
    try:
        assert client.get("/v2/patterns/view").json()["entries"] == view["entries"]
    finally:
        from logs_support import write_library

        write_library(built / "patterns")


def test_neighbourhood_on_the_logs_generation_names_seeds_and_invents_no_edges(client):
    """The synthetic logs generation projects signature documents only (no relations): the seed is
    found and named from the store, and no edge is drawn."""
    view = client.post("/v2/graph/neighbourhood", json={"query": "redis", "hops": 1}).json()
    assert view["seeds"] and view["edges"] == [] and view["truncated"] is False
    assert [(n["name"], n["type"]) for n in view["nodes"]] == [("redis", "dependency")]
    assert client.post("/v2/graph/neighbourhood", json={}).status_code == 422
    assert client.post("/v2/graph/neighbourhood", json={"query": "redis", "max_nodes": 301}).status_code == 422
    assert client.post("/v2/graph/neighbourhood", json={"query": "zzzz-no-such-entity"}).json() == {"nodes": [], "edges": [], "truncated": False, "seeds": []}
    unknown = edge_id("nobody", "fails_to_connect", "nothing", "fact")
    assert client.get(f"/v2/graph/edges/{unknown}").status_code == 404
    assert client.get("/v2/graph/edges/not-base64!").status_code == 422


def _span(doc, text, start=0):
    return Span(document_id=doc, chunk_id=f"{doc}#0", field="exemplars", start=start, end=start + len(text), text=text, match_level="exact")


class FakeGraph:
    """Contract fake of the graph extras: three entities, two stored fact edges (one seen twice)."""

    names = {"e1": ("db-client", "component"), "e2": ("redis", "dependency"), "e3": ("kafka", "dependency")}

    def __init__(self):
        prov = Provenance(extractor="pattern")
        first = PathEdge(subject_id="e1", predicate="fails_to_connect", object_id="e2", edge_kind="fact", support="trigger_in_span", span=_span("d1", "db-client failed to connect redis"), provenance=prov,
                         occurrences=[Occurrence(span=_span("d2", "db-client refused by redis"), provenance=prov)])
        second = PathEdge(subject_id="e1", predicate="fails_to_connect", object_id="e3", edge_kind="fact", support="trigger_in_span", span=_span("d3", "db-client refused by kafka"), provenance=prov)
        self.paths = [GraphPath(nodes=["e1", "e2"], edges=[first]), GraphPath(nodes=["e1", "e3"], edges=[second])]

    def find_entities(self, gen, tokens, scope):
        from su_multi_arm.providers import tokens as tokenize

        return [Entity(id=i, name=n, type=t, span=_span("d1", n)) for i, (n, t) in self.names.items() if set(tokenize(n)) <= set(tokens)]

    def traverse(self, gen, spec, scope):
        return [p for p in self.paths if set(p.nodes) & set(spec.seed_entity_ids) and p.edges[0].edge_kind in spec.edge_kinds and (not spec.predicates or p.edges[0].predicate in spec.predicates)][: spec.limit]

    def mentions(self, gen, ids, scope, limit):
        return [Entity(id=i, name=self.names[i][0], type=self.names[i][1], span=_span("d1", self.names[i][0])) for i in ids if i in self.names][:limit]


def test_neighbourhood_and_edge_provenance_with_a_store_that_has_edges():
    store, gen = FakeGraph(), None
    view = graph_neighbourhood(store, gen, NeighbourhoodRequest(query="db-client", hops=1))
    assert view["seeds"] == ["e1"] and {n["id"]: (n["name"], n["degree"]) for n in view["nodes"]} == {"e1": ("db-client", 2), "e2": ("redis", 1), "e3": ("kafka", 1)}
    assert sorted((e["source"], e["predicate"], e["target"], e["occurrences"]) for e in view["edges"]) == [("e1", "fails_to_connect", "e2", 1), ("e1", "fails_to_connect", "e3", 1)]
    edge = next(e for e in view["edges"] if e["target"] == "e2")
    detail = graph_edge(store, gen, parse_edge_id(edge["id"]))
    assert [s["quote"] for s in detail["spans"]] == ["db-client failed to connect redis", "db-client refused by redis"]
    assert detail["support"] == "trigger_in_span" and detail["extractor"] == "pattern"
    capped = graph_neighbourhood(store, gen, NeighbourhoodRequest(query="db-client", hops=1, max_nodes=2))
    assert capped["truncated"] is True and len(capped["nodes"]) == 2 and len(capped["edges"]) == 1
    assert graph_neighbourhood(store, gen, NeighbourhoodRequest(query="db-client", edge_kinds=["co_occurrence"]))["edges"] == []


def test_communities_absent_is_reported_not_zero(client):
    listing = client.get("/v2/communities").json()
    assert listing["available"] is False and listing["communities"] == [] and listing["reason"]
    assert client.get("/v2/communities/c-unknown").status_code == 404


class Reader:
    def resolve(self, headers):
        return Principal(id="reader", scopes=["search", "answer"])


def test_inspect_scope_and_single_tenant_rule(built):
    engine = Engine(validate_config(config_data(built)))
    reader = TestClient(create_app(engine=engine, principal_resolver=Reader()))
    assert reader.get("/v2/info").status_code == 403  # no inspect scope
    tenanted = Engine(validate_config(config_data(built)), acl=StaticPolicy("tenant"))
    response = TestClient(create_app(engine=tenanted)).get("/v2/info")
    assert response.status_code == 403 and "single-tenant" in response.json()["detail"]


def test_review_queue_lists_proposals_with_examples_gate_and_counts(tmp_path):
    """The owner's review view: noise proposals from ``proposed.yaml`` with their redacted examples,
    matched-line counts and gate results; a decided proposal shows its effective status; the route
    reads the files and writes nothing."""
    from su_multi_arm.extraction.patterns.library import Decision, PatternEntry
    from su_multi_arm.extraction.patterns.writer import append_decision, append_proposal

    from logs_support import write_library

    root = write_library(tmp_path / "patterns")
    gate = {"passed": True, "engine": "re2", "matched": 41, "checks": {"support": {"passed": True}, "out_of_cluster": {"passed": True, "detail": "0 lines"}}}
    for ident, regex, example in (
        ("noise.metrics_flush", r"^metrics flushed in [0-9]{1,4}ms$", "metrics flushed in 12ms"),
        ("noise.cache_warm", r"^cache warm <b>x</b> [0-9]{1,3}$", "cache warm <b>x</b> 7"),
    ):
        entry = PatternEntry(id=ident, kind="noise", status="proposed", regex=regex, examples={"positive": [example]}, evidence={"gate": gate}, provenance={"author": "system:learner"})
        append_proposal(root, entry)
    append_decision(root, Decision(op="approve", id="noise.cache_warm", by="user:alice", at=datetime(2026, 1, 20, tzinfo=timezone.utc), reason="benign"))
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    client = TestClient(create_app(engine=Engine(validate_config(config_data(tmp_path)))))
    review = client.get("/v2/patterns/review", params={"kind": "noise"}).json()
    assert review["available"] is True and review["source"] == "working_files" and "proposed.yaml" in review["file_shas"]
    rows = {r["id"]: r for r in review["proposals"]}
    assert set(rows) == {"noise.metrics_flush", "noise.cache_warm"}
    assert rows["noise.metrics_flush"]["status"] == "proposed" and rows["noise.cache_warm"]["status"] == "approved"
    flush = rows["noise.metrics_flush"]
    assert flush["examples"] == ["metrics flushed in 12ms"] and flush["matched_lines"] == 41
    assert flush["gate"]["checks"]["out_of_cluster"]["detail"] == "0 lines"
    assert review["proposal_status_counts"] == {"approved": 1, "proposed": 1}
    assert [(d["op"], d["id"], d["by"]) for d in review["decisions"]] == [("approve", "noise.cache_warm", "user:alice")]
    assert client.get("/v2/patterns/review", params={"kind": "message"}).json()["proposals"] == []
    assert client.get("/v2/patterns/review", params={"kind": "../x"}).status_code == 422
    assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()} == before
