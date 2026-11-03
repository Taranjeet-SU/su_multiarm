"""postgres adapter: pure SQL compilation (no server) and error mapping through a thin fake connection.

The fake exists only to prove that driver failures become typed ``BackendError``s with the adapter and
operation names, that a broken connection is discarded without retrying the failed operation, and
that input validation happens before any I/O; it is not evidence of behaviour against PostgreSQL. The
real-service evidence is ``tests/conformance/test_postgres_conformance.py`` (``external``)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import numpy as np
import pytest

psycopg = pytest.importorskip("psycopg")

from su_multi_arm.backends.postgres import PostgresBackend, _db  # noqa: E402
from su_multi_arm.backends.postgres.search import _match_sql, _with_cfg  # noqa: E402
from su_multi_arm.core.errors import BackendError  # noqa: E402
from su_multi_arm.core.types import FieldSpec, LexicalQuery, Page, Predicate, Scope  # noqa: E402
from su_multi_arm.testing import backend_conformance as kit  # noqa: E402

# --- naming, paging -----------------------------------------------------------------------------


def test_schema_names_are_generation_scoped_and_fit_postgres_identifiers():
    assert _db.schema_name("su", "conformance", "a" * 32) == "su_conformance_" + "a" * 32
    long = _db.schema_name("su_multi_arm", "a_dataset_name_that_is_long", "a" * 32)
    odd = _db.schema_name("su", "Mixed-Case/ds", "a" * 32)
    for name in (long, odd):
        assert len(name) <= 63 and name.startswith(("su_multi_arm_g", "su_g"))
    assert _db.schema_name("su", "Mixed-Case/ds", "a" * 32) != _db.schema_name("su", "Mixed-Case/ds", "b" * 32)
    assert _db.ident("su_x") == '"su_x"'
    for bad in ('x"; drop', "", "A", "a" * 64):
        with pytest.raises(BackendError, match="refusing identifier"):
            _db.ident(bad)
    for bad in ("1x", "X", "a-b", "a" * 42):
        with pytest.raises(BackendError, match="table_prefix"):
            _db.check_prefix(bad)
    assert _db.check_ts_config("pg_catalog.english") == "pg_catalog.english"
    with pytest.raises(BackendError, match="text_search_config"):
        _db.check_ts_config("english'; drop")


def test_page_window():
    assert _db.page_window(None, 10) == (0, 10)
    assert _db.page_window(Page(cursor="5", size=3), 10) == (5, 3)
    with pytest.raises(BackendError, match="k must be positive"):
        _db.page_window(None, 0)
    for cursor in ("-1", "x"):
        with pytest.raises(BackendError, match="invalid cursor"):
            _db.page_window(Page(cursor=cursor), 10)


# --- typed rows and predicates ------------------------------------------------------------------


def test_field_rows_use_native_types_and_the_utc_storage_rule():
    utc = timezone.utc
    assert _db.field_row("o", "s", "Ab") == ("o", "s", "string", "Ab", "ab", None, None, False)
    assert _db.field_row("o", "i", 2)[2:6] == ("integer", None, None, 2.0)
    assert _db.field_row("o", "b", True)[2:6] == ("boolean", "true", None, 1.0)
    assert _db.field_row("o", "n", None) == ("o", "n", "null", None, None, None, None, True)
    aware = _db.field_row("o", "t", "2024-01-02T01:00:00+01:00")
    assert aware[2] == "datetime" and aware[3] == "2024-01-02T01:00:00+01:00" and aware[6] == datetime(2024, 1, 2, tzinfo=utc)
    assert _db.field_row("o", "t", "2024-01-02T00:00:00")[6] == datetime(2024, 1, 2, tzinfo=utc)  # naive = UTC (assume_utc)
    assert _db.field_row("o", "d", "2024-03-05")[6] == datetime(2024, 3, 5, tzinfo=utc)  # a date = its midnight UTC
    assert [_db.row_as_string(*r) for r in (("integer", None, 2.0), ("number", None, 2.5), ("string", "x", None))] == ["2", "2.5", "x"]


def test_predicates_compile_to_bound_parameters_only():
    sql, params = _db.predicate_sql("s.record_fields", "r.id", Predicate(field="p", op="eq", value="X'; drop"), False)
    assert "drop" not in sql and params == ["p", "X'; drop"] and "f.text_value = %s" in sql
    sql, params = _db.predicate_sql("t", "r.id", Predicate(field="p", op="eq", value="X"), True)
    assert "f.fold_value = %s" in sql and params == ["p", "x"]
    sql, params = _db.predicate_sql("t", "r.id", Predicate(field="p", op="in", value=[]), False)
    assert "FALSE" in sql and params == ["p"]
    sql, _ = _db.predicate_sql("t", "r.id", Predicate(field="p", op="missing"), False)
    assert sql.startswith("NOT EXISTS")
    sql, _ = _db.predicate_sql("t", "r.id", Predicate(field="p", op="eq", value="X", negate=True), False)
    assert sql.startswith("NOT (EXISTS")
    day = FieldSpec(name="day", type="date")
    sql, params = _db.predicate_sql("t", "r.id", Predicate(field="day", op="eq", value="2024-03-05T10:00:00Z"), False, day)
    assert "f.ts_value >= %s AND f.ts_value < %s" in sql
    assert params[1:] == [datetime(2024, 3, 5, tzinfo=timezone.utc), datetime(2024, 3, 6, tzinfo=timezone.utc)]
    sql, params = _db.predicate_sql(
        "t", "r.id", Predicate(field="c", op="date_interval", lower="2024-01-01T00:00:00+00:00", upper="2024-01-02T00:00:00+00:00", granularity="day"), False
    )
    assert "f.ts_value >= %s" in sql and "f.ts_value < %s" in sql  # half-open
    sql, params = _db.predicate_sql("t", "r.id", Predicate(field="s", op="range", lower="b"), False)
    assert "f.kind='string' AND f.text_value >= %s" in sql and params == ["s", "b"]
    sql, _ = _db.predicate_sql("t", "r.id", Predicate(field="n", op="range", lower=True), False)
    assert "(FALSE)" in sql


def test_scope_sql():
    assert _db.scope_sql('"s"', "d.id", None) == ("TRUE", [])
    assert _db.scope_sql('"s"', "d.id", Scope(document_ids=[])) == ("FALSE", [])
    sql, params = _db.scope_sql('"s"', "d.id", Scope(document_ids=["a"], exclude_ids=["z"], predicates=[Predicate(field="p", op="eq", value="X")]))
    assert sql.startswith("d.id = ANY(%s) AND NOT (d.id = ANY(%s)) AND EXISTS") and '"s".document_fields' in sql
    assert params == [["a"], ["z"], "p", "X"]


def test_tsquery_parts_never_pass_user_syntax_through():
    parts = _db.tsquery_parts(LexicalQuery(text="cert & !round | 'x':* <-> )("))
    assert parts["terms"] == ["cert", "round", "x"]
    assert _db.tsquery_parts(LexicalQuery(text="&&& !!")) is None
    assert _db.tsquery_parts(LexicalQuery(kind="phrase", text="Exemption, certificate!"))["phrase"] == "exemption certificate"
    boolean = _db.tsquery_parts(LexicalQuery(kind="boolean", text="a", must=["b c", "!!"], must_not=["d"]))
    assert boolean["must"] == ["b c"] and boolean["must_not"] == ["d"]
    where, params, tq, tq_params = _match_sql(boolean, "%s::regconfig")
    assert where.count("phraseto_tsquery") == 4 and "NOT (l.title_tsv" in where
    assert params == ["a", "a", "b c", "b c", "d", "d"] and tq_params == ["a"]
    assert where.count("%s") == 2 * len(params) and _with_cfg(params, "english")[:2] == ["english", "a"]
    assert _db.vector_literal(np.array([1.0, 0.5], dtype=np.float32)) == "[1.0,0.5]"


# --- error mapping through a thin fake connection -----------------------------------------------


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.statements.append(sql)
        if self.conn.error is not None and self.conn.fail_on in sql:
            if self.conn.break_on_error:
                self.conn.broken = True
            raise self.conn.error

    def executemany(self, sql, rows):
        self.execute(sql)

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class FakeConnection:
    def __init__(self, error=None, fail_on="", break_on_error=False, lock_granted=True):
        self.error, self.fail_on, self.break_on_error = error, fail_on, break_on_error
        self.statements: list[str] = []
        self.closed = self.broken = False
        self.lock_granted = lock_granted

    @contextmanager
    def transaction(self):
        yield

    def cursor(self):
        return FakeCursor(self)

    def execute(self, sql, params=None):
        granted = self.lock_granted

        class Result:
            def fetchone(self):
                return (granted,)

        return Result()

    def close(self):
        self.closed = True


def make(*connections):
    pool = list(connections)
    made = []

    def connect():
        if not pool:
            raise psycopg.OperationalError("connection refused")
        conn = pool.pop(0)
        made.append(conn)
        return conn

    backend = PostgresBackend("postgresql://fake", "su", connect=connect)
    backend.made = made
    return backend


def test_unreachable_server_is_a_typed_error_with_the_operation():
    backend = make()
    with pytest.raises(BackendError) as info:
        backend.ledger.put_raw("k", "{", valid=False)
    assert (info.value.adapter, info.value.operation) == ("postgres", "put_raw") and "OperationalError" in str(info.value)


def test_driver_errors_are_mapped_and_never_retried():
    conn = FakeConnection(error=psycopg.errors.InsufficientPrivilege("permission denied"), fail_on="\"raw_replies\"(key")
    backend = make(conn)
    with pytest.raises(BackendError, match="InsufficientPrivilege") as info:
        backend.ledger.put_raw("k", "{", valid=False)
    assert info.value.operation == "put_raw"
    assert sum("\"raw_replies\"(key" in s for s in conn.statements if s.startswith("INSERT")) == 1  # no retry


def test_a_broken_connection_is_discarded_and_the_next_operation_reconnects():
    first = FakeConnection(error=psycopg.OperationalError("server closed the connection"), fail_on="SELECT value", break_on_error=True)
    second = FakeConnection()
    backend = make(first, second)
    with pytest.raises(BackendError, match="server closed"):
        backend.cache.get(kit.GEN.prefix() + "x")
    assert backend._conn is None
    assert backend.cache.get(kit.GEN.prefix() + "x") is None  # fresh connection, empty fake
    assert backend.made == [first, second]


def test_typed_errors_from_inside_a_transaction_pass_through_unchanged():
    backend = make(FakeConnection())
    with pytest.raises(BackendError, match="unknown generation") as info:
        backend.catalog.get(kit.GEN, ["d1"])
    assert info.value.operation == "generation"


def test_input_validation_happens_before_any_io():
    backend = make()  # any connection attempt would raise OperationalError
    for bad, match in (
        ((["x", "x"], np.ones((2, 2))), "duplicate ids"),
        ((["x"], np.ones((2, 2))), "one row per id"),
        ((["x"], np.full((1, 2), np.inf)), "finite"),
        ((["x"], np.ones((1, 16001))), "1..16000"),
    ):
        with pytest.raises(BackendError, match=match):
            backend.vector.upsert(kit.GEN, *bad)
    with pytest.raises(BackendError, match="GenerationId prefix"):
        backend.cache.put("answer:x", 1, 10)
    with pytest.raises(BackendError, match="ttl must be positive"):
        backend.cache.put(kit.GEN.prefix() + "x", 1, 0)
    record = kit.StructuredRecord(schema_id="document", id="bad", values={"created_at": "not a date"}, provenance=kit.PROV)
    with pytest.raises(BackendError, match="record bad"):
        backend.records.upsert(kit.GEN, [record], kit.SCHEMA)
    with pytest.raises(BackendError, match="1..2000"):
        PostgresBackend("postgresql://fake", "su", vector_index="hnsw", connect=lambda: None).vector.upsert(kit.GEN, ["x"], np.ones((1, 2001)))
    for kwargs, match in (({"timeout_seconds": 0}, "timeout"), ({"vector_index": "ivfflat"}, "unknown vector index"), ({"max_values_cardinality": 0}, "cardinality")):
        with pytest.raises(BackendError, match=match):
            PostgresBackend("postgresql://fake", "su", **kwargs)


def test_held_write_lock_is_a_typed_error_not_a_wait():
    backend = make(FakeConnection(), FakeConnection(lock_granted=False))
    with pytest.raises(BackendError, match="write lock is held"):
        with backend.generation_registry().lock():
            pass
    assert backend.made[1].closed


def test_protected_generation_is_never_dropped():
    backend = make()
    backend.protect(kit.GEN)
    with pytest.raises(BackendError, match="active or pinned"):
        backend.delete_generation(kit.GEN)
