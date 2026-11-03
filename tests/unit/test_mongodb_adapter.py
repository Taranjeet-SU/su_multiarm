"""mongodb adapter: pure query compilation (no server) and error mapping through a thin fake client.

The thin fake exists only to prove that driver failures become typed ``BackendError``s with the
adapter and operation names; it is not evidence of behaviour against MongoDB. The real-service
evidence is ``tests/conformance/test_mongodb_conformance.py`` (``external``)."""

from __future__ import annotations

import pytest

from su_multi_arm.backends import capabilities as caps
from su_multi_arm.backends.mongodb import _db
from su_multi_arm.core.errors import BackendError
from su_multi_arm.core.types import LexicalQuery, Page, Predicate, Scope
from su_multi_arm.testing import backend_conformance as kit

# --- naming, paging -----------------------------------------------------------------------------


def test_collection_names_are_generation_scoped_and_validated():
    assert _db.generation_name("su", "conformance", "a" * 32) == "su_conformance_" + "a" * 32
    for bad in ("", "a b", "x$y", "system.x", "a" * 65):
        with pytest.raises(BackendError, match="not a safe collection-name component"):
            _db.check_name(bad, "dataset")


def test_page_window():
    assert _db.page_window(None, 10) == (0, 10)
    assert _db.page_window(Page(cursor="5", size=3), 10) == (5, 3)
    with pytest.raises(BackendError, match="k must be positive"):
        _db.page_window(None, 0)
    with pytest.raises(BackendError, match="invalid cursor"):
        _db.page_window(Page(cursor="-1"), 10)
    with pytest.raises(BackendError, match="invalid cursor"):
        _db.page_window(Page(cursor="x"), 10)


# --- typed rows and predicates ------------------------------------------------------------------


def test_field_rows_classify_scalars_like_sqlite():
    rows = _db.field_rows({"s": "Ab", "i": 2, "f": 2.5, "b": True, "n": None, "t": "2024-01-02T00:00:00+00:00", "naive": "2024-01-02T00:00:00"})
    by = {r["name"]: r for r in rows}
    assert by["s"] == {"name": "s", "k": "string", "t": "Ab", "f": "ab", "n": None, "null": False}
    assert by["i"]["k"] == "integer" and by["i"]["n"] == 2.0 and by["f"]["k"] == "number"
    assert by["b"] == {"name": "b", "k": "boolean", "t": "true", "f": None, "n": 1.0, "null": False}
    assert by["n"]["null"] is True and by["n"]["k"] == "null"
    assert by["t"]["k"] == "datetime" and by["t"]["n"] == 1704153600.0
    # Storage rule (``_scope.instant``): a naive stamp can only arrive through an ``assume_utc`` field
    # and is stored as its UTC instant with canonical text.
    assert by["naive"]["k"] == "datetime" and by["naive"]["n"] == 1704153600.0 and by["naive"]["t"] == "2024-01-02T00:00:00+00:00"
    assert [_db.row_as_string(by[k]) for k in ("s", "i", "f", "b", "t")] == ["Ab", "2", "2.5", "true", "2024-01-02T00:00:00+00:00"]


def test_predicate_filters():
    assert _db.predicate_filter(Predicate(field="p", op="eq", value="X"), False) == {"fields": {"$elemMatch": {"name": "p", "t": "X"}}}
    assert _db.predicate_filter(Predicate(field="p", op="eq", value="X"), True) == {"fields": {"$elemMatch": {"name": "p", "f": "x"}}}
    assert _db.predicate_filter(Predicate(field="n", op="eq", value=3), False)["fields"]["$elemMatch"] == {"name": "n", "k": {"$in": ["integer", "number"]}, "n": 3.0}
    assert _db.predicate_filter(Predicate(field="b", op="eq", value=False), False)["fields"]["$elemMatch"] == {"name": "b", "k": "boolean", "n": 0.0}
    assert _db.predicate_filter(Predicate(field="x", op="eq", value=None), False)["fields"]["$elemMatch"] == {"name": "x", "null": True}
    assert _db.predicate_filter(Predicate(field="x", op="missing"), False) == {"fields": {"$not": {"$elemMatch": {"name": "x", "null": False}}}}
    assert _db.predicate_filter(Predicate(field="p", op="in", value=["A", 1]), True)["fields"]["$elemMatch"]["$or"] == [{"f": "a"}, {"k": {"$in": ["integer", "number"]}, "n": 1.0}]
    assert _db.predicate_filter(Predicate(field="p", op="in", value=[]), False) == _db.IMPOSSIBLE
    rng = _db.predicate_filter(Predicate(field="n", op="range", lower=1, upper=5, inclusive=False), False)
    assert rng["fields"]["$elemMatch"]["$and"] == [{"null": False}, {"k": {"$in": ["integer", "number"]}, "n": {"$gt": 1.0}}, {"k": {"$in": ["integer", "number"]}, "n": {"$lt": 5.0}}]
    half_open = _db.predicate_filter(Predicate(field="d", op="date_interval", lower="2024-01-01T00:00:00+00:00", upper="2024-01-02T00:00:00+00:00", granularity="day"), False)
    assert half_open["fields"]["$elemMatch"]["$and"][1:] == [{"k": "datetime", "n": {"$gte": 1704067200.0}}, {"k": "datetime", "n": {"$lt": 1704153600.0}}]
    text_bound = _db.predicate_filter(Predicate(field="s", op="range", lower="b"), False)
    assert text_bound["fields"]["$elemMatch"]["$and"][1] == {"k": "string", "t": {"$gte": "b"}}
    assert _db.predicate_filter(Predicate(field="n", op="range", lower=True), False) == _db.IMPOSSIBLE
    assert _db.predicate_filter(Predicate(field="p", op="eq", value="X", negate=True), False) == {"$nor": [{"fields": {"$elemMatch": {"name": "p", "t": "X"}}}]}
    assert _db.predicate_filter(Predicate(field="p", op="eq", value="X"), False, path="doc.fields") == {"doc.fields": {"$elemMatch": {"name": "p", "t": "X"}}}


def test_scope_filter():
    assert _db.scope_filter(None) == {}
    assert _db.scope_filter(Scope()) == {}
    assert _db.scope_filter(Scope(document_ids=[])) is None
    assert _db.scope_filter(Scope(document_ids=["a"]), document_key="document_id") == {"document_id": {"$in": ["a"]}}
    combined = _db.scope_filter(Scope(exclude_ids=["z"], predicates=[Predicate(field="p", op="eq", value="X")]), casefold=lambda f: True)
    assert combined == {"$and": [{"_id": {"$nin": ["z"]}}, {"fields": {"$elemMatch": {"name": "p", "f": "x"}}}]}
    assert _db.combine({}, {"a": 1}) == {"a": 1} and _db.combine({"a": 1}, {"b": 2}) == {"$and": [{"a": 1}, {"b": 2}]}


def _match_cond(row: dict, cond: dict) -> bool:
    """The subset of MongoDB query semantics ``_db`` emits, evaluated over one typed field row."""
    for key, want in cond.items():
        if key == "$and":
            if not all(_match_cond(row, c) for c in want):
                return False
        elif key == "$or":
            if not any(_match_cond(row, c) for c in want):
                return False
        elif isinstance(want, dict):
            value = row.get(key)
            for op, bound in want.items():
                if op == "$in":
                    ok = value in bound
                elif value is None or type(value) is not type(bound) and not all(isinstance(x, (int, float)) for x in (value, bound)):
                    ok = False
                else:
                    ok = {"$gt": value > bound, "$gte": value >= bound, "$lt": value < bound, "$lte": value <= bound}[op]
                if not ok:
                    return False
        elif row.get(key) != want:
            return False
    return True


def _match(doc: dict, query: dict) -> bool:
    for key, want in query.items():
        if key == "$and":
            ok = all(_match(doc, q) for q in want)
        elif key == "$nor":
            ok = not any(_match(doc, q) for q in want)
        elif key == "_id":
            ok = doc["_id"] in want["$in"] if "$in" in want else doc["_id"] not in want["$nin"]
        elif "$elemMatch" in want:
            ok = any(_match_cond(r, want["$elemMatch"]) for r in doc[key])
        else:
            ok = not any(_match_cond(r, want["$not"]["$elemMatch"]) for r in doc[key])
        if not ok:
            return False
    return True


def test_temporal_fields_compare_as_instants_like_the_shared_scope_rules():
    """Driver-free guard for the B1 storage rule: the compiled filters, evaluated over the stored
    typed rows, select exactly what ``_scope.matches`` (the memory reference) selects for the
    conformance kit's ``assume_utc`` / ``require_offset`` / ``date`` records."""
    from su_multi_arm.backends import _scope

    values = {
        "d1": {"created_at": "2024-01-01T00:00:00+00:00", "touched_at": "2025-01-02T08:00:00", "day": "2024-03-05"},
        "d2": {"created_at": "2024-01-02T00:00:00+00:00", "touched_at": "2025-01-02T09:00:00+01:00", "day": "2024-03-06"},
        "d3": {"created_at": None, "touched_at": "2025-01-01T23:30:00-02:00", "day": None},
    }
    docs = [{"_id": rid, "fields": _db.field_rows(v)} for rid, v in values.items()]
    predicates = [
        Predicate(field="touched_at", op="range", lower="2025-01-02T00:00:00+00:00"),
        Predicate(field="touched_at", op="range", lower="2025-01-02T08:00:00Z"),
        Predicate(field="touched_at", op="range", lower="2025-01-02T01:00:00"),
        Predicate(field="touched_at", op="range", lower="2025-01-02T02:00:00"),
        Predicate(field="touched_at", op="eq", value="2025-01-02T08:00:00+00:00"),
        Predicate(field="touched_at", op="in", value=["2025-01-02T01:30:00Z", "nope"]),
        Predicate(field="touched_at", op="eq", value="2025-01-02T08:00:00Z", negate=True),
        Predicate(field="created_at", op="date_interval", lower="2024-01-01T00:00:00+00:00", upper="2024-01-02T00:00:00+00:00", granularity="day"),
        Predicate(field="day", op="range", lower="2024-03-06T15:00:00+00:00"),
        Predicate(field="day", op="range", upper="2024-03-05T23:59:59+00:00"),
        Predicate(field="day", op="eq", value="2024-03-05T10:00:00Z"),
        Predicate(field="day", op="in", value=["2024-03-06"]),
        Predicate(field="day", op="range", lower="2024-03-05", upper="2024-03-06", inclusive=False),
    ]
    expected = [
        ["d1", "d2", "d3"], ["d1", "d2"], ["d1", "d2", "d3"], ["d1", "d2"], ["d1", "d2"], ["d3"], ["d3"],
        ["d1"], ["d2"], ["d1"], ["d1"], ["d2"], [],
    ]
    for predicate, want in zip(predicates, expected):
        spec = kit.SCHEMA.field(predicate.field)
        reference = [rid for rid, v in values.items() if _scope.matches(v, predicate, spec=spec)]
        compiled = _db.predicate_filter(predicate, False, spec=spec)
        got = [d["_id"] for d in docs if _match(d, compiled)]
        assert got == reference == want, predicate
    # A non-temporal value against a temporal field never matches; a date field equality is a day window.
    assert _db.predicate_filter(Predicate(field="day", op="eq", value="soon"), False, spec=kit.SCHEMA.field("day")) == _db.IMPOSSIBLE
    day_eq = _db.predicate_filter(Predicate(field="day", op="eq", value="2024-03-05T10:00:00Z"), False, spec=kit.SCHEMA.field("day"))
    assert day_eq["fields"]["$elemMatch"] == {"name": "day", "k": "datetime", "n": {"$gte": 1709596800.0, "$lt": 1709683200.0}}


# --- text search string -------------------------------------------------------------------------


def test_text_search_strings_never_carry_user_operators():
    assert _db.text_search(LexicalQuery(text='certificate "rounding" -error')) == "certificate rounding error"
    assert _db.text_search(LexicalQuery(kind="phrase", text="Exemption, certificate!")) == '"exemption certificate"'
    boolean = LexicalQuery(kind="boolean", text="rounding", must=["content source"], must_not=["error", "x y"])
    assert _db.text_search(boolean) == 'rounding "content source" -error -x -y'
    assert _db.text_search(LexicalQuery(text="!!!")) is None and _db.text_search(LexicalQuery(kind="phrase", text="...")) is None
    assert len(_db.text_search(LexicalQuery(text=" ".join(f"w{i}" for i in range(300)))).split()) == _db.MAX_TERMS


# --- thin fake client: error mapping only -------------------------------------------------------


class FakeCollection:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def __getattr__(self, method):
        def call(*args, **kwargs):
            self.store.calls.append((self.name, method))
            if self.store.error is not None:
                raise self.store.error
            if method == "find_one":
                return self.store.find_one
            return None

        return call


class FakeDatabase:
    def __init__(self, store):
        self.store = store

    def __getitem__(self, name):
        return FakeCollection(self.store, name)


class FakeClient:
    def __init__(self, error=None, find_one=None):
        self.error, self.find_one, self.calls = error, find_one, []

    def __getitem__(self, name):
        return FakeDatabase(self)

    def server_info(self):
        if self.error is not None:
            raise self.error
        return {"version": "fake"}


def make(client):
    from su_multi_arm.backends.mongodb import MongoBackend

    return MongoBackend("mongodb://unused", "db", "pfx", clock=lambda: 100.0, client=client)


def test_init_validates_names_and_exposes_no_vector_facet():
    pytest.importorskip("pymongo", reason="optional extra `mongodb` not installed")
    backend = make(FakeClient())
    assert set(caps.MATRIX["mongodb"].capabilities) - {"vector"} <= set(dir(backend)) and not hasattr(backend, "vector")
    assert backend.probe() == {"version": "fake", "database": "db", "prefix": "pfx"}
    from su_multi_arm.backends.mongodb import MongoBackend

    with pytest.raises(BackendError, match="collection_prefix"):
        MongoBackend("mongodb://unused", "db", "x$y", client=FakeClient())
    with pytest.raises(BackendError, match="timeout_seconds"):
        MongoBackend("mongodb://unused", "db", "pfx", timeout_seconds=0, client=FakeClient())


def test_unknown_generation_is_typed_without_touching_data_collections():
    pytest.importorskip("pymongo", reason="optional extra `mongodb` not installed")
    client = FakeClient(find_one=None)
    backend = make(client)
    with pytest.raises(BackendError) as info:
        backend.catalog.get(kit.GEN, ["d1"])
    assert (info.value.adapter, info.value.operation) == ("mongodb", "generation")
    assert client.calls == [("pfx_generations", "find_one")]


def test_driver_failures_map_to_backend_errors_with_operation_names():
    errors = pytest.importorskip("pymongo.errors")
    backend = make(FakeClient(error=errors.ServerSelectionTimeoutError("no server")))
    for operation, call in (
        ("generation", lambda: backend.catalog.get(kit.GEN, ["d1"])),
        ("index", lambda: backend.cache.get(kit.GEN.prefix() + "k")),  # first touch creates the TTL index
        ("replay", lambda: backend.ledger.replay("job")),
        ("get", lambda: backend.session.get("s")),
        ("ping", backend.ping),
        ("ping", backend.probe),  # doctor --probe reports this typed error
        ("current", backend.generation_registry().current),
    ):
        with pytest.raises(BackendError) as info:
            call()
        assert (info.value.adapter, info.value.operation) == ("mongodb", operation)
        assert "ServerSelectionTimeoutError" in str(info.value)
    refused = make(FakeClient(error=errors.OperationFailure("not authorized")))
    with pytest.raises(BackendError, match="OperationFailure: not authorized") as info:
        refused.ledger.put_raw("k", "{", valid=False)
    assert info.value.operation == "put_raw"


def test_cache_guards_run_before_any_round_trip():
    client = FakeClient()
    backend = make(client)
    with pytest.raises(BackendError, match="GenerationId prefix"):
        backend.cache.put("answer:x", 1, 10)
    with pytest.raises(BackendError, match="ttl must be positive"):
        backend.cache.put(kit.GEN.prefix() + "x", 1, 0)
    assert client.calls == []


def test_from_config_requires_the_environment_variable(monkeypatch):
    from su_multi_arm.backends.mongodb import MongoBackend
    from su_multi_arm.core.config import MongoBackend as MongoBackendConfig

    monkeypatch.delenv("SU_MONGO_TEST_UNSET", raising=False)
    spec = MongoBackendConfig(kind="mongodb", uri_env="SU_MONGO_TEST_UNSET", database="db")
    with pytest.raises(BackendError, match="SU_MONGO_TEST_UNSET"):
        MongoBackend.from_config(spec)


def test_matrix_rows_match_the_adapter():
    assert caps.serves("mongodb", "catalog") and caps.serves("mongodb", "lexical", "phrase") and caps.serves("mongodb", "records")
    assert caps.serves("mongodb", "cache") and caps.serves("mongodb", "ledger") and caps.serves("mongodb", "session")
    assert not caps.serves("mongodb", "vector") and not caps.declares("mongodb", "graph")
    assert caps.flags("mongodb", "lexical")["analyzer_id"] == "mongo_text_english"
