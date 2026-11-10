"""H1 history rows on a real OpenSearch (``external``): the gold set's day shards are written to a
history collection tagged ``shard_id``; every log tool answers exactly like the sqlite history
tier; gc deletes a dead shard's rows by query. Synthetic gold data only. Uses
``SU_TEST_OPENSEARCH_URL`` (default stack in ``tests/services``); every index it creates carries a
random prefix and is deleted at the end."""

from __future__ import annotations

import secrets

import pytest

from service_probe import require_search_service

from tests.logs_gold_support import AS_OF, QUESTIONS, build_gold, gold_config

pytestmark = pytest.mark.external
D = AS_OF.isoformat()


def comparable(output):
    data = output.model_dump(mode="json")
    return {k: data[k] for k in ("rows", "rendering", "exactness", "status", "source_unavailable")}


def test_history_rows_on_opensearch_answer_like_sqlite(tmp_path, monkeypatch):
    pytest.importorskip("opensearchpy")
    url = require_search_service("opensearch")
    monkeypatch.setenv("SU_TEST_H1_HOSTS", url)
    prefix = f"su_test_h1_{secrets.token_hex(3)}"
    data = gold_config(tmp_path / "os")
    data["backends"]["os"] = {"kind": "opensearch", "hosts_env": "SU_TEST_H1_HOSTS", "index_prefix": prefix}
    data["logs"]["history"] = {"tier": True, "backend": "os"}
    engine, pipe, report = build_gold(tmp_path / "os", data=data)
    sqlite_engine, _, _ = build_gold(tmp_path / "sq")
    try:
        assert type(engine.history).__name__ == "RecordsHistoryStore"
        assert report["history_rows"] == sqlite_engine.history.row_count()
        for _, intent, args in QUESTIONS:
            tool_args = args if intent == "log_around" else {**args, "as_of": D}
            assert comparable(engine.call_tool(intent, tool_args).result.output) == comparable(sqlite_engine.call_tool(intent, tool_args).result.output), intent
        view = engine.call_tool("graph_neighbourhood", {"entity": "svc-api"}).result.output
        assert view.edges and all(e.weights for e in view.edges)
        shards = engine.manifest()["logs"]["history"]["shards"]
        keep = set(list(shards.values())[1:])
        dead = engine.history.gc(keep)
        assert dead == [list(shards.values())[0]]
        from su_multi_arm.core.types import Predicate, ScopeSpec

        left = engine.backends["os"].records.execute(engine.history.gen, ScopeSpec(predicates=[Predicate(field="shard_id", op="in", value=dead)]), [])
        assert left.total == 0
    finally:
        engine.backends["os"].transport.request("DELETE", f"/{prefix}_*", params={"ignore_unavailable": "true"})
