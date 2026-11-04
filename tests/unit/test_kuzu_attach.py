"""``backends/kuzu`` attach mode: an existing Kuzu database (synthetic, built here with its own
schema) mapped read-only onto the graph contract through a declared mapping file."""

from __future__ import annotations

import pytest
import yaml

from su_multi_arm.arms.traversal import run_template
from su_multi_arm.core.config_graph import GraphIntent, TraversalTemplate
from su_multi_arm.core.errors import BackendError
from su_multi_arm.core.types import GenerationId, Scope, TraversalSpec

kuzu = pytest.importorskip("kuzu")

GEN = GenerationId(dataset="ext", generation="a" * 32, index_format=3, fingerprint="f" * 64)
OTHER = GEN.model_copy(update={"generation": "b" * 32})
PATTERN = r"^(case:[A-Z0-9]+)"

SCHEMA = [
    "CREATE NODE TABLE Ticket(id STRING, title STRING, body STRING, PRIMARY KEY(id))",
    "CREATE NODE TABLE Symptom(id STRING, name STRING, PRIMARY KEY(id))",
    "CREATE NODE TABLE Cause(id STRING, name STRING, PRIMARY KEY(id))",
    "CREATE NODE TABLE Fix(id STRING, name STRING, PRIMARY KEY(id))",
    "CREATE NODE TABLE Product(id STRING, name STRING, PRIMARY KEY(id))",
    "CREATE NODE TABLE Grp(id STRING, title STRING, lvl INT64, summary STRING, quote STRING, quote_source STRING, PRIMARY KEY(id))",
    "CREATE REL TABLE HAS_SYMPTOM(FROM Ticket TO Symptom, quote STRING, source_id STRING, verified BOOLEAN, method STRING, model STRING, at STRING, conf DOUBLE)",
    "CREATE REL TABLE HAS_CAUSE(FROM Ticket TO Cause, quote STRING, source_id STRING, verified BOOLEAN, method STRING, model STRING, at STRING, conf DOUBLE)",
    "CREATE REL TABLE RESOLVED_BY(FROM Ticket TO Fix, FROM Cause TO Fix, quote STRING, source_id STRING, verified BOOLEAN, method STRING, model STRING, at STRING, conf DOUBLE)",
    "CREATE REL TABLE ABOUT(FROM Ticket TO Product, quote STRING, source_id STRING, verified BOOLEAN, method STRING, model STRING, at STRING, conf DOUBLE)",
    "CREATE REL TABLE IN_GROUP(FROM Ticket TO Grp, FROM Product TO Grp)",
]


def _edge(rel, a_table, a, b_table, b, quote, source, verified=True, conf=0.8):
    return (
        f"MATCH (a:{a_table} {{id: '{a}'}}), (b:{b_table} {{id: '{b}'}}) CREATE (a)-[:{rel} {{quote: '{quote}', source_id: '{source}', "
        f"verified: {str(verified).lower()}, method: 'llm', model: 'm-1', at: '2026-01-01', conf: {conf}}}]->(b)"
    )


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    path = tmp_path_factory.mktemp("ext") / "ext.kuzu"
    db = kuzu.Database(str(path))
    conn = kuzu.Connection(db)
    for statement in SCHEMA:
        conn.execute(statement)
    for statement in [
        "CREATE (:Ticket {id: 'case:T1', title: 'Login fails after upgrade', body: 'Users cannot log in after the upgrade'})",
        "CREATE (:Ticket {id: 'case:T2', title: 'Login slow', body: 'Login takes minutes'})",
        "CREATE (:Symptom {id: 'sym:login-timeout', name: 'Login Timeout'})",
        "CREATE (:Cause {id: 'cause:token-expiry', name: 'Token Expiry'})",
        "CREATE (:Fix {id: 'fix:token-refresh', name: 'Token Refresh'})",
        "CREATE (:Fix {id: 'fix:cache-flush', name: 'Cache Flush'})",
        "CREATE (:Fix {id: 'fix:reboot', name: 'Reboot'})",
        "CREATE (:Product {id: 'product:portal', name: 'Portal'})",
        "CREATE (:Grp {id: 'grp:1', title: 'Login problems', lvl: 0, summary: 'Login problems after upgrades', quote: 'cannot log in', quote_source: 'case:T1#body'})",
        _edge("HAS_SYMPTOM", "Ticket", "case:T1", "Symptom", "sym:login-timeout", "cannot log in", "case:T1#body"),
        _edge("HAS_CAUSE", "Ticket", "case:T1", "Cause", "cause:token-expiry", "the token had expired", "case:T1#res"),
        _edge("RESOLVED_BY", "Cause", "cause:token-expiry", "Fix", "fix:token-refresh", "refreshing the token fixed it", "case:T1#res"),
        _edge("RESOLVED_BY", "Ticket", "case:T1", "Fix", "fix:token-refresh", "refreshing the token fixed it", "case:T1#res"),
        _edge("RESOLVED_BY", "Ticket", "case:T1", "Fix", "fix:reboot", "a reboot was tried", "case:T1#c2", verified=False),  # unverified: never surfaced
        _edge("ABOUT", "Ticket", "case:T1", "Product", "product:portal", "", "case:T1#body"),  # empty quote: never surfaced
        _edge("ABOUT", "Ticket", "case:T2", "Product", "product:portal", "the Portal login page", "case:T2#body", conf=7.0),  # bad confidence -> None
        _edge("HAS_SYMPTOM", "Ticket", "case:T2", "Symptom", "sym:login-timeout", "Login takes minutes", "case:T2#body"),
        _edge("RESOLVED_BY", "Ticket", "case:T2", "Fix", "fix:cache-flush", "flushing the cache helped", "case:T2#res"),
        _edge("RESOLVED_BY", "Ticket", "case:T2", "Fix", "fix:cache-flush", "flushing again", "elsewhere:9"),  # no document id: never surfaced
        "MATCH (t:Ticket), (g:Grp) CREATE (t)-[:IN_GROUP]->(g)",
        "MATCH (p:Product), (g:Grp) CREATE (p)-[:IN_GROUP]->(g)",
    ]:
        conn.execute(statement)
    conn.close()
    db.close()
    return path


MAPPING = {
    "dataset": "ext",
    "generations": ["a" * 32],
    "nodes": [
        {"table": "Ticket", "type": "issue", "name": "title", "document": {"column": "id", "pattern": PATTERN}, "mention_text": "body"},
        {"table": "Symptom", "type": "symptom"},
        {"table": "Cause", "type": "cause"},
        {"table": "Fix", "type": "fix"},
        {"table": "Product", "type": "product"},
    ],
    "edges": [
        {"table": t, "predicate": p, "document_pattern": PATTERN, "verified": "verified", "provenance": {"extractor": "method", "model": "model", "extracted_at": "at", "confidence": "conf"}}
        for t, p in (("HAS_SYMPTOM", "has_symptom"), ("HAS_CAUSE", "has_root_cause"), ("RESOLVED_BY", "resolved_by"), ("ABOUT", "about"))
    ],
    "communities": {"table": "Grp", "label": ["title"], "level": "lvl", "summary": "summary", "quote": "quote", "quote_source": "quote_source", "quote_document_pattern": PATTERN, "member_edge": "IN_GROUP"},
}


def attach(store, tmp_path, **overrides):
    from su_multi_arm.backends.kuzu import AttachMapping, KuzuAttachedBackend

    mapping = AttachMapping.model_validate({**MAPPING, **overrides})
    return KuzuAttachedBackend(store, mapping, buffer_pool_mb=64)


@pytest.fixture
def backend(store, tmp_path):
    b = attach(store, tmp_path)
    yield b
    b.close()


def nodes(paths):
    return [p.nodes for p in paths]


def test_traverse_maps_edges_with_quotes_documents_and_provenance(backend):
    paths = backend.graph.traverse(GEN, TraversalSpec(seed_entity_ids=["Ticket:case:T1"], hops=1, direction="out"), None)
    assert nodes(paths) == [
        ["Ticket:case:T1", "Symptom:sym:login-timeout"],
        ["Ticket:case:T1", "Cause:cause:token-expiry"],
        ["Ticket:case:T1", "Fix:fix:token-refresh"],
    ]  # the unverified reboot fix and the empty-quote product edge are not surfaced
    edge = paths[2].edges[0]
    assert (edge.predicate, edge.edge_kind, edge.support) == ("resolved_by", "fact", "unchecked")
    assert (edge.span.document_id, edge.span.chunk_id, edge.span.field, edge.span.text, edge.span.start, edge.span.end) == (
        "case:T1", "case:T1#res", "RESOLVED_BY.quote", "refreshing the token fixed it", 0, 29,
    )
    assert (edge.provenance.extractor, edge.provenance.model, edge.provenance.extracted_at, edge.provenance.confidence) == ("llm", "m-1", "2026-01-01", 0.8)
    t2 = backend.graph.traverse(GEN, TraversalSpec(seed_entity_ids=["Ticket:case:T2"], hops=1, direction="out", predicates=["about"]), None)
    assert nodes(t2) == [["Ticket:case:T2", "Product:product:portal"]] and t2[0].edges[0].provenance.confidence is None


def test_two_hops_direction_scope_and_unknown_seeds(backend):
    spec = TraversalSpec(seed_entity_ids=["Symptom:sym:login-timeout"], hops=2, direction="any", limit=50)
    everything = nodes(backend.graph.traverse(GEN, spec, None))
    assert ["Symptom:sym:login-timeout", "Ticket:case:T2", "Fix:fix:cache-flush"] in everything
    scoped = nodes(backend.graph.traverse(GEN, spec, Scope(exclude_ids=["case:T2"])))
    assert scoped and all("Ticket:case:T2" not in p for p in scoped)
    assert backend.graph.traverse(GEN, spec, Scope(document_ids=[])) == []
    assert backend.graph.traverse(GEN, TraversalSpec(seed_entity_ids=["Ghost:x", "nocolon"], hops=1), None) == []
    with pytest.raises(BackendError, match="predicates"):
        from su_multi_arm.core.types import Predicate

        backend.graph.traverse(GEN, spec, Scope(predicates=[Predicate(field="source", op="eq", value="x")]))
    assert backend.graph.traverse(GEN, TraversalSpec(seed_entity_ids=["Ticket:case:T1"], hops=1, edge_kinds=["co_occurrence"]), None) == []


def test_batched_reads_fall_back_to_per_node_reads_with_the_same_result(store, tmp_path, backend):
    spec = TraversalSpec(seed_entity_ids=["Symptom:sym:login-timeout"], hops=2, direction="any", limit=50)
    small = attach(store, tmp_path, max_rows_per_hop=1)
    try:
        assert small.graph.traverse(GEN, spec, None) == backend.graph.traverse(GEN, spec, None)
        assert small.graph.mentions(GEN, ["Fix:fix:token-refresh", "Product:product:portal"], None, 10) == backend.graph.mentions(GEN, ["Fix:fix:token-refresh", "Product:product:portal"], None, 10)
    finally:
        small.close()


def test_find_entities_and_mentions(backend):
    found = backend.graph.find_entities(GEN, ["users", "see", "login", "timeout", "token", "expiry"], None)
    assert sorted(e.id for e in found) == ["Cause:cause:token-expiry", "Symptom:sym:login-timeout"]
    assert {e.type for e in found} == {"cause", "symptom"}
    # under a scope, an entity is found only with an eligible mention
    assert backend.graph.find_entities(GEN, ["portal"], Scope(document_ids=["case:T1"])) == []
    assert [e.id for e in backend.graph.find_entities(GEN, ["portal"], Scope(document_ids=["case:T2"]))] == ["Product:product:portal"]
    mentions = backend.graph.mentions(GEN, ["Ticket:case:T1", "Fix:fix:token-refresh"], None, 10)
    assert mentions[0].span.text == "Users cannot log in after the upgrade" and mentions[0].span.field == "Ticket.body"
    assert {(m.id, m.span.text) for m in mentions[1:]} >= {("Fix:fix:token-refresh", "refreshing the token fixed it")}
    assert len(backend.graph.mentions(GEN, ["Ticket:case:T1", "Fix:fix:token-refresh"], None, 2)) == 2
    assert backend.graph.mentions(GEN, ["Ticket:case:T1"], Scope(exclude_ids=["case:T1"]), 10) == []


def test_communities_and_memberships(backend):
    communities = backend.graph.communities(GEN)
    assert [c.id for c in communities] == ["grp:1"]
    c = communities[0]
    assert c.label == "Login problems" and c.document_ids == ["case:T1", "case:T2"] and c.entity_ids == ["Product:product:portal"]
    assert c.status() == "external" and c.report["level"] == 0
    assert c.report["claims"] == [{"text": "Login problems after upgrades", "evidence": [{"document_id": "case:T1", "quote": "cannot log in"}]}]
    assert sorted(m.member_id for m in backend.graph.memberships(GEN)) == ["case:T1", "case:T2"]


def test_read_only_generation_scope_and_fixed_statements(backend):
    from su_multi_arm.testing import backend_conformance as kit

    entities, relations = kit.graph_fixture()
    with pytest.raises(BackendError, match="read-only"):
        backend.graph.upsert(GEN, entities, relations, [])
    with pytest.raises(BackendError, match="read-only"):
        backend.graph.put_communities(GEN, [])
    with pytest.raises(BackendError, match="read-only"):
        backend.delete_generation(GEN)
    with pytest.raises(BackendError, match="read-only"):
        with backend.session.transaction("x"):
            pass
    with pytest.raises(BackendError, match="unknown generation"):
        backend.graph.traverse(OTHER, TraversalSpec(seed_entity_ids=["Ticket:case:T1"]), None)
    with pytest.raises(BackendError, match="unknown generation"):
        backend.graph.find_entities(GEN.model_copy(update={"dataset": "other"}), ["portal"], None)
    with pytest.raises(BackendError, match="fixed template"):
        backend.run("x", "MATCH (n) RETURN n")
    assert backend.ping()["mode"] == "attach"


def test_ontology_and_templates_on_an_attached_store(backend):
    ontology = backend.graph.ontology()
    assert set(ontology.entity_types) == {"issue", "symptom", "cause", "fix", "product"}
    assert backend.graph.entity_types(["Fix:fix:x", "Nope:y", "plain"]) == {"Fix:fix:x": "fix"}
    assert set(ontology.predicates["resolved_by"].subject_types) == {"issue", "cause"}
    template = TraversalTemplate(
        kind="chain", seed_match="tokens", seed_types=["symptom"],
        steps=[{"predicates": ["has_symptom"], "direction": "in"}, {"predicates": ["resolved_by"], "direction": "out"}],
    )
    assert template.problems(ontology) == []
    result = run_template(backend.graph, GEN, "troubleshoot", template, GraphIntent(template="troubleshoot", seeds=["login timeout"]), None)
    assert [p.nodes[-1] for p in result.paths] == ["Fix:fix:token-refresh", "Fix:fix:cache-flush"]
    bad = TraversalTemplate(kind="chain", steps=[{"predicates": ["has_symptom"], "direction": "out"}, {"predicates": ["resolved_by"], "direction": "out"}], seed_types=["issue"])
    assert any("cannot follow" in p for p in bad.problems(ontology))
    assert any("unknown predicates" in p for p in TraversalTemplate(kind="neighbors", hop={"predicates": ["nope"]}).problems(ontology))


@pytest.mark.parametrize(
    "change, message",
    [
        ({"nodes": [{"table": "Nope", "type": "x"}]}, "does not exist"),
        ({"nodes": [{"table": "Ticket", "type": "issue", "name": "missing_col"}]}, "no column"),
        ({"edges": [{"table": "IN_GROUP", "predicate": "in_group"}]}, "no column"),
        ({"nodes": [{"table": "Ticket", "type": "issue", "name": "title"}], "edges": [{"table": "HAS_CAUSE", "predicate": "c"}]}, "no connection"),
        ({"communities": {"table": "Grp", "member_edge": "NOPE", "label": ["title"]}}, "does not exist"),
    ],
)
def test_mapping_is_validated_against_the_store(store, tmp_path, change, message):
    with pytest.raises(BackendError, match=message):
        attach(store, tmp_path, **change).close()


def test_mapping_file_rules(tmp_path):
    from su_multi_arm.backends.kuzu import AttachMapping

    for data, message in [
        ({**MAPPING, "generations": []}, "declare the package generations"),
        ({**MAPPING, "generations": ["XYZ"]}, "32 lowercase hex"),
        ({**MAPPING, "nodes": [*MAPPING["nodes"], {"table": "Other", "type": "issue"}]}, "exactly one node table"),
        ({**MAPPING, "edges": [{"table": "X", "predicate": "p", "support": "entailment_checked"}]}, "reserved"),
        ({**MAPPING, "edges": [{"table": "X", "predicate": "p", "document_pattern": "^(a)", "document_template": "\\1:\\2"}]}, "capturing groups"),
        ({**MAPPING, "edges": [{"table": "X", "predicate": "p", "document_pattern": "^a"}]}, "capturing groups"),
    ]:
        path = tmp_path / "m.yaml"
        path.write_text(yaml.safe_dump(data))
        with pytest.raises(BackendError, match="invalid mapping"):
            AttachMapping.load(path)
    path.write_text(yaml.safe_dump({**MAPPING, "any_generation": True, "generations": []}))
    assert AttachMapping.load(path).any_generation
    with pytest.raises(BackendError, match="cannot read mapping"):
        AttachMapping.load(tmp_path / "missing.yaml")


def test_attach_never_creates_a_database(tmp_path):
    from su_multi_arm.backends.kuzu import AttachMapping, KuzuAttachedBackend

    with pytest.raises(BackendError, match="never creates"):
        KuzuAttachedBackend(tmp_path / "absent.kuzu", AttachMapping.model_validate(MAPPING))
    assert not (tmp_path / "absent.kuzu").exists()


def test_document_rules_expand_templates():
    from su_multi_arm.backends.kuzu.attach import DocRule, document_of

    rule = DocRule(r"^(guide):(?:[^/#]*/)?(\d+)", r"\1:\2")
    assert document_of(rule, "guide:Admin/104853#s2") == "guide:104853" and document_of(rule, "guide:77") == "guide:77"
    assert document_of(rule, "vocab:products") is None and document_of(rule, None) is None and document_of(None, "x") is None
