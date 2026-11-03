"""neo4j adapter without a server (plan §3.5 ``neo4j`` row, §2.1.3): only the fixed parameterised
templates ever reach the driver and request values travel as parameters; the hop bound and the
document-id-only eligibility are enforced before any statement runs; fact traversal cannot reach a
co-occurrence relationship type; driver/server failures become a typed ``BackendError`` and roll the
transaction back; configuration, activation and naming refusals. Real-service behaviour is covered by
``tests/conformance/test_neo4j_conformance.py`` and ``tests/services/test_neo4j_engine.py``."""

from __future__ import annotations

import pytest

from su_multi_arm.backends import capabilities as caps
from su_multi_arm.backends.neo4j import Neo4jBackend
from su_multi_arm.backends.neo4j import _cypher as q
from su_multi_arm.core.config import Neo4jBackend as Neo4jBackendConfig
from su_multi_arm.core.errors import BackendError
from su_multi_arm.core.types import Predicate, Scope, TraversalSpec
from su_multi_arm.testing import backend_conformance as kit

HOSTILE = "x'}) MATCH (n) DETACH DELETE n //"


class FakeRecord:
    def __init__(self, data):
        self._data = data

    def data(self):
        return dict(self._data)


class FakeTx:
    def __init__(self, driver):
        self.driver, self.committed, self.rolled_back = driver, False, False

    def run(self, statement, params):
        self.driver.calls.append((statement, params))
        if self.driver.fail is not None:
            raise self.driver.fail
        return [FakeRecord(r) for r in self.driver.respond(statement, params)]

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def closed(self):
        return self.committed or self.rolled_back


class FakeSession:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def begin_transaction(self, timeout=None):
        tx = FakeTx(self.driver)
        self.driver.transactions.append(tx)
        return tx

    def run(self, statement, params):
        self.driver.calls.append((statement, params))
        return []


class FakeDriver:
    def __init__(self, respond=None):
        self.calls, self.transactions, self.fail = [], [], None
        self.respond = respond or self.default

    @staticmethod
    def default(statement, params):
        if statement == q.GENERATION_STATE:
            return [{"state": "loaded", "seq": 0}]
        if statement == q.GENERATION_OPEN:
            return [{"seq": 0}]
        if statement.lstrip().startswith("UNWIND $rows AS row\nMATCH (s:"):
            return [{"written": len(params["rows"])}]
        return []

    def session(self, **kwargs):
        return FakeSession(self)

    def close(self):
        pass


def backend(driver=None):
    return Neo4jBackend("bolt://unused:1", "neo4j", "pw", namespace="unit", driver=driver or FakeDriver())


def test_matrix_row_is_on_the_contracts_with_memberships_and_no_predicate_scope():
    assert caps.status_of("neo4j", "graph") == "contracts" and caps.MATRIX["neo4j"].status == "contracts"
    assert caps.serves("neo4j", "graph", "memberships") and caps.flags("neo4j", "graph")["predicate_scope"] is False
    assert set(caps.MATRIX["neo4j"].capabilities) == {"graph"}
    assert kit.check_protocols(backend()) == {"graph"}


def test_only_fixed_templates_run_and_values_are_parameters():
    driver = FakeDriver()
    b = backend(driver)
    entities, relations = kit.graph_fixture()
    hostile = entities[0].model_copy(update={"id": HOSTILE, "name": HOSTILE})
    b.graph.upsert(kit.GEN, [*entities, hostile], relations, [])
    b.graph.traverse(kit.GEN, TraversalSpec(seed_entity_ids=[HOSTILE, "aacta"], hops=2, predicates=[HOSTILE]), Scope(document_ids=[HOSTILE], exclude_ids=[HOSTILE]))
    b.graph.find_entities(kit.GEN, [HOSTILE], Scope(exclude_ids=[HOSTILE]))
    b.graph.mentions(kit.GEN, [HOSTILE], None, 5)
    b.graph.memberships(kit.GEN)
    b.graph.communities(kit.GEN)
    statements = [s for s, _ in driver.calls]
    assert statements and set(statements) <= set(q.ALL_TEMPLATES)
    assert all(HOSTILE not in s for s in statements)
    assert any(HOSTILE in str(p) for _, p in driver.calls)  # it travelled, as a parameter
    assert all(tx.committed for tx in driver.transactions)


def test_templates_are_bounded_and_fact_traversal_cannot_reach_co_occurrence():
    fact = q.HOP[frozenset({"fact"})]
    assert "SU_FACT" in fact and "SU_CO_OCCURRENCE" not in fact
    assert set(q.HOP) == {frozenset(k) for k in q.HOP}
    assert len(q.HOP) == 2 ** len(q.REL_TYPES) - 1
    for template in q.HOP.values():
        assert "*" not in template  # one hop per statement; no variable-length pattern
        assert "$allow" in template and "$exclude" in template
    assert "{" not in "".join(q.REL_TYPES.values())


def test_refusal_of_non_template_statements():
    b = backend()
    with pytest.raises(BackendError, match="fixed template"):
        with b.transaction("x", write=False) as run:
            run("MATCH (n) DETACH DELETE n")


@pytest.mark.parametrize("hops", [4, -1, "2", 2.0, True, None])
def test_hop_bound_is_validated_before_any_statement(hops):
    driver = FakeDriver()
    spec = TraversalSpec.model_construct(seed_entity_ids=["a"], hops=hops, predicates=[], direction="any", edge_kinds=["fact"], limit=10)
    with pytest.raises(BackendError, match="hops"):
        backend(driver).graph.traverse(kit.GEN, spec, None)
    assert driver.calls == []


def test_predicates_are_refused_never_ignored():
    driver = FakeDriver()
    graph = backend(driver).graph
    scope = Scope(predicates=[Predicate(field="source", op="eq", value="case")])
    for call in (
        lambda: graph.traverse(kit.GEN, TraversalSpec(seed_entity_ids=["a"]), scope),
        lambda: graph.find_entities(kit.GEN, ["a"], scope),
        lambda: graph.mentions(kit.GEN, ["a"], scope, 3),
    ):
        with pytest.raises(BackendError, match="document_ids"):
            call()
    assert driver.calls == []


def test_unknown_generation_is_typed():
    driver = FakeDriver(lambda statement, params: [])
    with pytest.raises(BackendError, match="unknown generation"):
        backend(driver).graph.memberships(kit.GEN)


def test_driver_failures_are_typed_and_roll_back():
    exceptions = pytest.importorskip("neo4j.exceptions")
    for error in (exceptions.ConstraintError("constraint"), exceptions.ServiceUnavailable("down"), exceptions.AuthError("auth")):
        driver = FakeDriver()
        b = backend(driver)
        b._schema_ready = True
        driver.fail = error
        entities, relations = kit.graph_fixture()
        with pytest.raises(BackendError) as info:
            b.graph.upsert(kit.GEN, entities, relations, [])
        assert info.value.adapter == "neo4j" and info.value.operation == "upsert"
        assert driver.transactions and all(tx.rolled_back and not tx.committed for tx in driver.transactions)


def test_dangling_endpoint_rolls_the_whole_call_back():
    def respond(statement, params):
        if statement == q.MISSING_ENDPOINTS:
            return [{"k": "ghost"}]
        return FakeDriver.default(statement, params)

    driver = FakeDriver(respond)
    entities, relations = kit.graph_fixture()
    with pytest.raises(BackendError, match="ghost"):
        backend(driver).graph.upsert(kit.GEN, entities, relations, [])
    assert driver.transactions[-1].rolled_back and not driver.transactions[-1].committed
    assert q.GENERATION_CLOSE not in [s for s, _ in driver.calls]


def test_activation_without_a_committed_load_is_refused():
    with pytest.raises(BackendError, match="no committed load"):
        backend(FakeDriver(lambda s, p: [])).activate_generation(kit.GEN)


def test_delete_is_refused_for_protected_and_pointed_generations():
    b = backend(FakeDriver(lambda s, p: [{"generation": kit.GEN.generation, "g": "unit|conformance|" + kit.GEN.generation}] if s == q.POINTER_READ else []))
    with pytest.raises(BackendError, match="active"):
        b.delete_generation(kit.GEN)
    b.protect(kit.GEN2)
    with pytest.raises(BackendError, match="pinned"):
        b.delete_generation(kit.GEN2)


def test_configuration_and_naming(monkeypatch):
    config = Neo4jBackendConfig(kind="neo4j", password_env="SU_UNIT_NEO4J_PASSWORD_UNSET")
    monkeypatch.delenv("SU_UNIT_NEO4J_PASSWORD_UNSET", raising=False)
    with pytest.raises(BackendError, match="password_env"):
        Neo4jBackend.from_config(config)
    with pytest.raises(BackendError, match="namespace"):
        Neo4jBackend("bolt://unused:1", "neo4j", "pw", namespace="bad|name", driver=FakeDriver())
    with pytest.raises(BackendError, match="dataset"):
        backend().gen_key(kit.GEN.model_copy(update={"dataset": "a|b"}))
    with pytest.raises(ValueError):
        Neo4jBackendConfig(kind="neo4j", namespace="bad|name")
    assert Neo4jBackendConfig(kind="neo4j").namespace == "su_multi_arm"
