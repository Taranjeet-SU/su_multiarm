"""Arms as components (plan §3.8) on both contract adapters: filters before selection, document
dedup, grounded traversal, community membership, ArmResult shape, refusal on incomplete stages,
Python-side injection through ``Engine(config, components=[...])``, v1 Config acceptance."""

from __future__ import annotations

import json

import pytest

from su_multi_arm import Document, Engine, SearchRequest
from su_multi_arm.arms import ArmInput, ArmResult, BaseArm, IncompleteStageError, builtin_registry
from su_multi_arm.core.component import Dependencies, Environment, is_component_class
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.core.types import Measure, Predicate, Scope, ScopeSpec
from su_multi_arm.models import Aggregate, Hit
from su_multi_arm.providers import Embedder

BASE = {
    "schema_version": 2,
    "dataset": "demo",
    "backends": {"local": {"kind": "memory"}},
    "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
    "metadata_schema": {"fields": [{"name": "product", "type": "string", "groupable": True}, {"name": "created_at", "type": "datetime"}]},
    "pipeline": {"arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local", "phrase_boost": {"enabled": True}}, "structured": {"backend": "local"}, "graph": {"backend": "local"}, "community": {"backend": "local"}}},
    "extraction": {"extractors": ["gazetteer"], "gazetteer": {"topic": ["exemption certificate", "rounding"], "product": ["CloudTax", "SearchDesk"]}},
    "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}}},
}

DOCS = [
    Document(id="kb:certificate", dataset="demo", source="article", title="Add an exemption certificate", fields={"details": "Open the record and add the exemption certificate. CloudTax rounding applies per line."}, metadata={"product": "CloudTax", "created_at": "2025-01-01T00:00:00+00:00"}, url="https://example.org/1"),
    Document(id="case:certificate", dataset="demo", source="case", title="Certificate expired", fields={"description": "An exemption certificate stopped applying in CloudTax."}, metadata={"product": "CloudTax", "created_at": "2025-02-03T12:00:00+00:00"}, url="https://example.org/2"),
    Document(id="kb:rounding", dataset="demo", source="article", title="Rounding configuration", fields={"details": "CloudTax rounding applies per invoice line."}, metadata={"product": "CloudTax"}, url="https://example.org/3"),
    Document(id="kb:source", dataset="demo", source="manual", title="Create a content source", fields={"body": "In SearchDesk a content source connects a repository to the index."}, metadata={"product": "SearchDesk"}, url="https://example.org/4"),
]


def config(kind, tmp_path, **overrides):
    data = json.loads(json.dumps(BASE))
    data["backends"]["local"] = {"kind": "memory"} if kind == "memory" else {"kind": "sqlite", "path": str(tmp_path / "state")}
    for key, value in overrides.items():
        data[key] = value
    return validate_config(data)


@pytest.fixture(params=["memory", "sqlite"])
def engine(request, tmp_path):
    engine = Engine(config(request.param, tmp_path))
    engine.ingest(DOCS)
    return engine


def test_every_arm_returns_an_arm_result_with_hits_or_aggregates(engine):
    ctx = engine._context_for(None)
    scope, acl = engine._scope(ctx, SearchRequest(query="x"), {})
    for name in ("dense", "lexical", "graph", "community"):
        result = engine.arms[name].run(ctx, ArmInput(query="exemption certificate", scope=scope, acl=acl, candidates=50))
        assert isinstance(result, ArmResult) and result.arm == name and not result.incomplete
        assert "kb:certificate" in {h.document_id for h in result.hits}
        assert all(isinstance(h, Hit) and h.arms == [name] and h.chunk_ids and h.provenance for h in result.hits)
        assert len({h.document_id for h in result.hits}) == len(result.hits)  # collapsed to documents
    structured = engine.arms["structured"].run(ctx, ArmInput(query="count", scope=scope, acl=acl, aggregate=ScopeSpec(measure=Measure(kind="count"))))
    assert structured.aggregates[0]["value"] == 4 and structured.aggregates[0]["exactness"] == "exact" and structured.hits == []
    assert engine.arms["structured"].run(ctx, ArmInput(query="count", scope=scope, acl=acl)).aggregates == []


def test_filters_and_exclusions_apply_inside_every_arm_before_selection(engine):
    ctx = engine._context_for(None)
    request = SearchRequest(query="exemption certificate", filters={"source": "case"}, exclude_ids=["kb:rounding"])
    scope, acl = engine._scope(ctx, request, {})
    for name in ("dense", "lexical", "graph", "community"):
        result = engine.arms[name].run(ctx, ArmInput(query=request.query, scope=scope, acl=acl, candidates=50))
        assert {h.source for h in result.hits} <= {"case"}, name
    counted = engine.arms["structured"].run(ctx, ArmInput(query="n", scope=scope, acl=acl, aggregate=ScopeSpec(measure=Measure(kind="count"))))
    assert counted.aggregates[0]["value"] == 1 and counted.aggregates[0]["document_ids"] == ["case:certificate"]
    # ACL predicates narrow the denominator; request filters never widen it
    acl_only = [Predicate(field="product", op="eq", value="SearchDesk")]
    narrowed = engine.arms["structured"].run(ctx, ArmInput(query="n", scope=Scope(predicates=acl_only), acl=acl_only, aggregate=ScopeSpec(measure=Measure(kind="count"))))
    assert (narrowed.aggregates[0]["value"], narrowed.aggregates[0]["denominator"]) == (1, 1)
    grouped = engine.arms["structured"].run(ctx, ArmInput(query="n", scope=Scope(), acl=[], aggregate=ScopeSpec(group_by=["product"])))
    assert grouped.aggregates[0]["value"] == {"CloudTax": 3, "SearchDesk": 1} and grouped.aggregates[0]["operation"] == "group_count"
    with pytest.raises(ValueError, match="allowlisted"):
        engine.arms["structured"].run(ctx, ArmInput(query="n", scope=Scope(), acl=[], aggregate=ScopeSpec(group_by=["secret"])))


def test_lexical_phrase_boost_ranks_contiguous_phrase_first_and_dense_rejects_zero_vector(engine):
    ctx = engine._context_for(None)
    lexical = engine.arms["lexical"].run(ctx, ArmInput(query="exemption certificate", scope=Scope(), candidates=50))
    assert lexical.hits[0].document_id in {"kb:certificate", "case:certificate"}
    assert engine.arms["lexical"].run(ctx, ArmInput(query="!!!", scope=Scope())).hits == []
    assert engine.arms["dense"].run(ctx, ArmInput(query="!!!", scope=Scope())).hits == []


def test_graph_and_community_membership_are_grounded_mentions(engine):
    ctx = engine._context_for(None)
    graph = engine.arms["graph"].run(ctx, ArmInput(query="CloudTax rounding", scope=Scope(), candidates=50))
    assert graph.hits and all(p["quote"] and p["match_level"] == "exact" and p["depth"] == 0 for h in graph.hits for p in h.provenance)
    assert graph.paths == []  # gazetteer extraction yields mentions, no facts: nothing to traverse
    assert engine.arms["graph"].run(ctx, ArmInput(query="nothing here", scope=Scope())).hits == []
    community = engine.arms["community"].run(ctx, ArmInput(query="rounding", scope=Scope(), candidates=50))
    assert community.hits and all(p["kind"] == "membership_not_causality" and p["report_status"] == "disabled" for h in community.hits for p in h.provenance)
    assert {r["label"] for r in community.reports} and all(r["claims"] == [] for r in community.reports)
    scoped = engine.arms["community"].run(ctx, ArmInput(query="rounding", scope=Scope(exclude_ids=["kb:rounding", "kb:certificate"]), candidates=50))
    assert all(h.document_id not in {"kb:rounding", "kb:certificate"} for h in scoped.hits)


def test_arms_refuse_incomplete_stages_unless_allowed(tmp_path, monkeypatch):
    cfg = config("memory", tmp_path, extraction={**BASE["extraction"], "policy": "partial"})
    engine = Engine(cfg)
    monkeypatch.setattr(Embedder, "encode", lambda self, texts, query=False: (_ for _ in ()).throw(RuntimeError("provider down")))
    manifest = engine.ingest(DOCS, allow_partial=True)
    assert manifest["status"] == "partial" and manifest["coverage"]["stages"]["embeddings"]["status"] == "incomplete"
    monkeypatch.undo()
    with pytest.raises(ValueError, match="incomplete"):
        engine.search("certificate")  # dense depends on embeddings: typed refusal, never an empty result
    ctx = engine._context_for(None)
    with pytest.raises(IncompleteStageError):
        engine.arms["dense"].run(ctx, ArmInput(query="certificate", scope=Scope(), incomplete_stages=["embeddings"]))
    lexical = engine.arms["lexical"].run(ctx, ArmInput(query="certificate", scope=Scope(), incomplete_stages=["embeddings"]))
    assert lexical.hits and not lexical.incomplete
    engine.config.pipeline.allow_incomplete_arms = True
    result = engine.search("certificate")
    assert result.trace["coverage"] == {"incomplete": True, "stages": ["embeddings"]}
    assert next(s for s in result.trace["steps"] if s["arm"] == "dense")["incomplete"] is True
    answer = engine.answer("certificate")
    assert answer.trace["coverage"]["incomplete"] is True and answer.trace["coverage"]["stages"]["embeddings"]["status"] == "incomplete"
    assert answer.coverage is not None and answer.coverage.policy == "partial" and answer.coverage.incomplete_stages == ["embeddings"]
    assert answer.coverage.documents_total == 4 and "embeddings incomplete" in (answer.coverage.note or "")


class PinnedLexical(BaseArm):
    """An injected arm: same name as the built-in, pins one record to the front."""

    name = "lexical"
    config_model = builtin_registry().get("lexical").config_model
    capability = "lexical"

    def _run(self, ctx, inputs):
        inner = builtin_registry().get("lexical")(self.cfg, self.deps).run(ctx, inputs)
        inner.hits.sort(key=lambda h: (h.document_id != "kb:source", -h.score))
        return inner


def test_python_components_are_injected_only_through_the_api(tmp_path):
    cfg = config("memory", tmp_path)
    assert is_component_class(PinnedLexical)
    engine = Engine(cfg, components=[PinnedLexical])
    engine.ingest(DOCS)
    assert isinstance(engine.arms["lexical"], PinnedLexical)
    hits = engine.search("content source certificate").arm_hits["lexical"]
    assert hits[0].document_id == "kb:source"
    instance = PinnedLexical(cfg.pipeline.arms.lexical, Dependencies(backends=engine.backends, components={"catalog": engine.backends["local"].catalog}, metadata_schema=cfg.metadata_schema))
    assert instance.validate(Environment()) == []
    assert isinstance(Engine(cfg, components=[instance]).arms["lexical"], PinnedLexical)
    with pytest.raises(ConfigError, match="Component contract"):
        Engine(cfg, components=[object()])

    class NotAnArm:
        name, kind, config_model = "x", "guard", PinnedLexical.config_model

        def validate(self, env, probe=False):
            return []

        def run(self, ctx, inputs):
            return None

        def recipe_fingerprint(self):
            return None

    with pytest.raises(ConfigError, match="only arm components and decision-agent instances"):
        Engine(cfg, components=[NotAnArm])


def test_engine_accepts_v1_config(tmp_path):
    from su_multi_arm.compat.config_v1 import Config as ConfigV1

    v1 = ConfigV1(dataset="demo", storage_path=tmp_path / "v1state", extraction={"providers": ["tags"], "tags": {"topic": ["rounding"]}})
    engine = Engine(v1)
    assert engine.config.schema_version == 2 and engine.config.backends["local"].path == tmp_path / "v1state"
    engine.ingest(DOCS)
    assert engine.search(SearchRequest(query="rounding", aggregate=Aggregate(operation="count"))).aggregates[0]["value"] == 4


def test_engine_builds_postgres_and_sql_backends_lazily_and_fails_closed_without_a_dsn(tmp_path, monkeypatch):
    """postgres and sqlalchemy are on the contracts: the engine builds them (no connection at
    construction) and a missing DSN/URL variable is a typed error, never a silent fallback."""
    pytest.importorskip("sqlalchemy")
    from su_multi_arm.backends.postgres import PostgresBackend
    from su_multi_arm.backends.sql import SqlBackend
    from su_multi_arm.core.errors import BackendError

    data = json.loads(json.dumps(BASE))
    data["backends"] = {
        "local": {"kind": "sqlite", "path": str(tmp_path / "s")},
        "pg": {"kind": "postgres", "dsn_env": "SU_TEST_UNSET_PG_DSN"},
        "rel": {"kind": "sqlalchemy", "url_env": "SU_TEST_SQL_URL"},
    }
    data["pipeline"]["arms"]["dense"] = {"backend": "pg"}
    data["storage"]["records"] = "rel"
    monkeypatch.delenv("SU_TEST_UNSET_PG_DSN", raising=False)
    monkeypatch.setenv("SU_TEST_SQL_URL", f"sqlite:///{tmp_path / 'rel.db'}")
    with pytest.raises(BackendError, match="SU_TEST_UNSET_PG_DSN"):
        Engine(validate_config(data))
    monkeypatch.setenv("SU_TEST_UNSET_PG_DSN", "postgresql://127.0.0.1:1/never_contacted")
    engine = Engine(validate_config(data))
    assert isinstance(engine.backends["pg"], PostgresBackend) and isinstance(engine.backends["rel"], SqlBackend)
    assert engine.backends["pg"]._conn is None  # construction never connects


def test_engine_end_to_end_with_sqlalchemy_storage(tmp_path, monkeypatch):
    """records, ledger, sessions and cache on the generic SQL adapter (SQLite dialect); catalog and the
    search arms on the local sqlite backend (sqlalchemy has no lexical/vector facet)."""
    pytest.importorskip("sqlalchemy")
    data = json.loads(json.dumps(BASE))
    data["backends"] = {"local": {"kind": "sqlite", "path": str(tmp_path / "s")}, "rel": {"kind": "sqlalchemy", "url_env": "SU_TEST_SQL_URL", "table_prefix": "e2e"}}
    data["storage"].update({"records": "rel", "ledger": "rel", "sessions": "rel", "cache": "rel"})
    data["pipeline"]["arms"]["structured"] = {"backend": "rel"}
    monkeypatch.setenv("SU_TEST_SQL_URL", f"sqlite:///{tmp_path / 'rel.db'}")
    engine = Engine(validate_config(data))
    manifest = engine.ingest(DOCS)
    assert manifest["projections"]["records"] == {"backend": "rel", "rows": 4, "verified": True}
    assert engine.search(SearchRequest(query="rounding", aggregate=Aggregate(operation="count"))).aggregates[0]["value"] == 4
    assert engine.backends["rel"].records.count(engine.write_path.current()[0]) == 4
    assert "kb:certificate" in {h.document_id for h in engine.search("exemption certificate").hits}
