"""Graph and community arms fed from the pattern extractor (addendum A2 §4.1.3–4.1.4, L5a/L5b).

* every header-link edge has span provenance whose quote names both endpoints, with per-day weights
  from the day documents it was seen in; ``co_fires_with`` edges come from the incident header;
* line-scoped facts come from exemplar lines of reviewed entries only (B7);
* co-firing incidents are identical across processes with different ``PYTHONHASHSEED`` (B9) and
  recover the planted incident with no planted-independent pair joined.
Synthetic gold set; evidence level: contract.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from su_multi_arm.testing.fixtures.logs.gold import INCIDENT, plan, pulled_days

from tests.logs_gold_support import SPEC, build_gold
from tests.logs_support import ENGINE
from tests.pattern_fixtures import engine_or_skip


@pytest.fixture(scope="module")
def gold(tmp_path_factory):
    engine, _, report = build_gold(tmp_path_factory.mktemp("graph"))
    return engine, report


def view(engine, entity, **kw):
    return engine.call_tool("graph_neighbourhood", {"entity": entity, **kw}).result.output


def names(engine, view_):
    return {n.id: n.name for n in view_.nodes}


@pytest.mark.parametrize("service", ["svc-api", "svc-billing", "svc-auth"])
def test_service_edges_have_span_provenance_and_day_weights(gold, service):
    engine, _ = gold
    v = view(engine, service)
    labels = names(engine, v)
    predicates = Counter(e.predicate for e in v.edges)
    assert predicates["emits"] >= 1 and predicates["serves"] >= 1 and predicates["runs"] == 4  # four tenants run it
    for edge in v.edges:
        assert edge.edge_kind == "native_link" and edge.support == "unchecked"
        assert labels[edge.subject] in edge.quote and labels[edge.object] in edge.quote  # span names both endpoints
        assert edge.document_id.startswith("logs:") and edge.weights and all(w["lines"] > 0 for w in edge.weights)
    emits = [e for e in v.edges if e.predicate == "emits" and labels[e.object] == "http.request"]
    assert {w["date"] for w in emits[0].weights} == {d.isoformat() for d in pulled_days(SPEC)}


def test_signature_raises_exception_class(gold):
    engine, _ = gold
    v = view(engine, "handler.unhandled_exception")
    labels = names(engine, v)
    raised = sorted(labels[e.object] for e in v.edges if e.predicate == "raises")
    assert raised == ["KeyError", "TimeoutError"]


def test_line_scoped_facts_from_exemplars(gold):
    engine, _ = gold
    v = view(engine, "db-client", edge_kinds=["fact"])
    facts = [e for e in v.edges if e.predicate == "fails_to_connect"]
    assert facts and all(e.edge_kind == "fact" and e.support == "trigger_in_span" and "failed to connect" in e.quote for e in facts)


def test_co_firing_edges_are_not_traversed_by_default(gold):
    engine, _ = gold
    assert not [e for e in view(engine, "cache.evicted").edges if e.predicate == "co_fires_with"]
    v = view(engine, "cache.evicted", edge_kinds=["co_firing"])
    labels = names(engine, v)
    pairs = {tuple(sorted((labels[e.subject], labels[e.object]))) for e in v.edges}
    assert pairs == {("cache.evicted", "queue.backlog"), ("cache.evicted", "worker.restarted")}
    assert all("lift" in e.quote and e.edge_kind == "co_firing" for e in v.edges) and "not causality" in v.note


def test_incident_is_a_community(gold):
    engine, _ = gold
    graph = engine.backends["local"].graph
    gen = engine.write_path.current()[0]
    communities = graph.communities(gen)
    assert len(communities) == 1 and communities[0].id.startswith("cofire:")
    assert communities[0].stats["signatures"] == ",".join(sorted(INCIDENT))


def test_unreviewed_entry_relations_are_co_occurrence(tmp_path):
    """B7: a learned (auto-promoted, not human-approved) entry never yields a fact edge."""
    from su_multi_arm.core.config import PatternLibraryRef
    from su_multi_arm.core.types import Chunk, Document
    from su_multi_arm.extraction.patterns.extractor import PatternExtractor
    from su_multi_arm.predicate_support import PredicateSupport
    import yaml

    from su_multi_arm.testing.fixtures.logs.gold import LIBRARY

    root = tmp_path / "patterns"
    root.mkdir()
    user = {**LIBRARY, "patterns": [p for p in LIBRARY["patterns"] if p["id"] != "msg.dependency.connect_failed"]}
    learned = [dict(next(p for p in LIBRARY["patterns"] if p["id"] == "msg.dependency.connect_failed"), provenance={"author": "system:policy"})]
    (root / "library.yaml").write_text(yaml.safe_dump(user, sort_keys=False))
    (root / "learned.yaml").write_text(yaml.safe_dump(learned, sort_keys=False))
    engine_name = ENGINE
    engine_or_skip(engine_name)
    extractor = PatternExtractor(PatternLibraryRef(path=root, engine=engine_name), PredicateSupport("english", {}))
    text = "db-client failed to connect redis after 100 ms"
    document = Document(id="d1", dataset="logs", kind="signature_examples", fields={"exemplars": text})
    result = extractor.extract(Chunk(id="c1", document_id="d1", field="exemplars", start=0, end=len(text), text=text, recipe="line"), document)
    assert result.relations and all(r.edge_kind == "co_occurrence" and r.support == "unchecked" for r in result.relations)


# --- determinism across processes (B9) --------------------------------------------------------------

SCRIPT = """
import json, sys
from collections import Counter
from su_multi_arm.communities.cooccurrence import co_firing
from su_multi_arm.core.config_logs import CoFiringConfig
from su_multi_arm.testing.fixtures.logs.gold import GoldSpec, plan, pulled_days
spec = GoldSpec(); days = set(pulled_days(spec))
cells = Counter()
for e in plan(spec):
    if e.day in days and e.signature and e.status == "parsed":
        cells[(e.signature, e.tenant, "prod", e.service, f"{e.day.isoformat()}T{e.hour:02d}:00:00+00:00")] += 1
rows = [{"signature_id": s, "tenant": t, "env": v, "service": svc, "hour": h, "lines": n} for (s, t, v, svc, h), n in sorted(cells.items(), reverse=True)]
report = co_firing(rows, CoFiringConfig())
print(json.dumps([[i.community_id, i.tenant, i.signatures, i.top_pairs] for i in report.incidents]))
"""


def test_co_firing_is_identical_across_hash_seeds():
    import su_multi_arm

    source = str(Path(su_multi_arm.__file__).resolve().parents[1])
    outputs = []
    for seed in ("0", "1", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": source, "PYTHONDONTWRITEBYTECODE": "1"}
        done = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, check=True)
        outputs.append(done.stdout)
    assert outputs[0] == outputs[1] == outputs[2]
    incidents = json.loads(outputs[0])
    assert [(t, sigs) for _, t, sigs, _ in incidents] == [("t02", sorted(INCIDENT))]  # planted recovered, nothing else joined


def test_plan_has_no_other_sparse_cofiring():
    """Guard on the fixture itself: the planted incident's hours hold only its own signatures."""
    hours = {(e.tenant, e.day, e.hour) for e in plan(SPEC) if e.signature in INCIDENT}
    others = {e.signature for e in plan(SPEC) if (e.tenant, e.day, e.hour) in hours and e.signature not in INCIDENT and e.signature}
    assert others == set()
