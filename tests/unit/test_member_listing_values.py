"""Listing, observed values, denominators and the typed projection per member (unified merge M1b,
§6.1 facet table, §6.8 attach list) over an adopted member, equal to today's engine on the store.

Synthetic (``tests/adopted_support.py``). Also the M1b regression found on real data: a hit read
that mixes chunk ids remembered from an earlier search with unknown ones keeps the caller's order,
so each document's first chunk (the one that carries text) is today's.
"""

from __future__ import annotations

import pytest

from su_multi_arm import Engine
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import BackendError, ConfigError
from su_multi_arm.core.types import LexicalQuery, Page, Predicate, Scope
from su_multi_arm.projection.catalog import ProjectionCatalog
from su_multi_arm.projection.spec import ProjectionSpec
from su_multi_arm.projection.store import attach_list, open_readonly, served_generation
from tests.adopted_support import adopted_engine, build_original, todays_engine, unified_data


@pytest.fixture(scope="module")
def engines(tmp_path_factory):
    root = tmp_path_factory.mktemp("listing")
    original = build_original(root)
    return adopted_engine(root, original), todays_engine(original), root, original


def _pins(engines):
    unified, today, *_ = engines
    return unified, today, unified.write_path.pin()[0], today.write_path.pin()[0]


@pytest.mark.parametrize("size", [1, 5, 50])
@pytest.mark.parametrize("scope", [None, Scope(predicates=[Predicate(field="source", op="eq", value="ticket")]), Scope(document_ids=["ticket:1003", "https://docs.example.org/kb/search-errors", "ticket:404"])])
def test_listing_pages_equal_todays(engines, size, scope):
    unified, today, ga, gb = _pins(engines)

    def listing(engine, gen):
        out, cursor = [], None
        while True:
            page = engine.backends["local"].catalog.iter_scope(gen, scope, Page(cursor=cursor, size=size))
            out.extend(d.model_dump() for d in page.items)
            if page.next_cursor is None:
                return out
            cursor = page.next_cursor

    assert listing(unified, ga) == listing(today, gb)


def test_observed_values_documents_and_denominators_equal_todays(engines):
    unified, today, ga, gb = _pins(engines)
    for field in ("source", "status", "service", "account", "priority"):
        assert unified.records.values(ga, field, [], 100) == today.records.values(gb, field, [], 100)
        assert unified.records.values(ga, field, [], 2) == today.records.values(gb, field, [], 2)
    assert unified.records.exists(ga, "status", "open", []) and not unified.records.exists(ga, "status", "nope", [])
    a, b = unified.backends["local"], today.backends["local"]
    ids = ["ticket:1001", "https://docs.example.org/kb/getting-started", "ticket:999"]
    assert a.catalog.get(ga, ids) == b.catalog.get(gb, ids)
    assert a.catalog.count_documents(ga) == b.catalog.count_documents(gb) == 18
    assert a.catalog.count_chunks(ga) == b.catalog.count_chunks(gb)
    assert unified.records.count(ga) == today.records.count(gb)


def test_hit_reads_mixing_remembered_and_unknown_chunks_keep_the_callers_order(engines):
    unified, today, ga, gb = _pins(engines)
    a, b = unified.backends["local"], today.backends["local"]
    remembered = a.lexical.search(ga, LexicalQuery(kind="terms", text="crawler timeout"), None, 3).items
    assert remembered and all(a.chunk_owner(h.chunk_id) for h in remembered)
    with b.catalog_db(gb) as db:
        rows = db.execute("SELECT id, document_id FROM chunks ORDER BY document_id, id").fetchall()
    by_doc: dict[str, list[str]] = {}
    for cid, doc in rows:
        by_doc.setdefault(doc, []).append(cid)
    mixed = []
    for hit in remembered:
        others = [c for c in by_doc[hit.document_id] if c != hit.chunk_id and a.chunk_owner(c) is None]
        mixed.extend([*others[:1], hit.chunk_id])  # an unknown chunk of the same document first
    assert any(a.chunk_owner(c) is None for c in mixed)
    assert a.catalog.hit_parts(ga, mixed) == b.catalog.hit_parts(gb, mixed)


def test_the_projection_is_the_adopted_members_own(engines):
    unified, today, ga, gb = _pins(engines)
    facet, theirs = unified.backends["local"].relational, today.backends["local"].relational
    spec = ProjectionSpec.build(unified.config.projection, unified.config.metadata_schema, None)
    assert served_generation(facet, ga) == gb and served_generation(theirs, gb) == gb
    left, right = ProjectionCatalog.load(facet, ga, spec), ProjectionCatalog.load(theirs, gb, spec)
    assert left is not None and left == right
    assert facet.file_names(ga) == theirs.file_names(gb)
    name = next(n for n in facet.file_names(ga) if n.endswith(".sqlite"))
    sql = "SELECT status, count(*) FROM ticket GROUP BY status ORDER BY status"
    rows = [open_readonly(f, g, name, left.grant(), 5000).execute(sql).fetchall() for f, g in ((facet, ga), (theirs, gb))]
    assert rows[0] == rows[1] and rows[0]
    with pytest.raises(BackendError):
        facet.put_file(ga, name, b"")


def test_the_attach_list_names_every_member_projection_file(engines):
    unified, today, ga, gb = _pins(engines)
    ours, theirs = attach_list(unified.backends["local"].relational, ga), attach_list(today.backends["local"].relational, gb)
    assert [item["path"] for item in ours] == [item["path"] for item in theirs] and ours
    assert all(item["role"] == "member" and item["unit"].startswith("support/adopted:") and item["alias"].startswith("support_0_") for item in ours)
    assert all(item["role"] == "projection" for item in theirs)


def test_a_projection_over_several_non_log_members_is_refused(engines):
    """M4 completion: the projection is one non-log member's; two projection candidates still refuse."""
    _, _, root, original = engines
    data = unified_data(root, original)
    data["collection"]["members"]["tickets"] = {"kind": "ticket", "sources": ["ticket"], "record_kinds": ["ticket"], "id_namespaces": ["case:"]}
    with pytest.raises(ConfigError, match="typed projection is served for exactly one non-log member"):
        Engine(validate_config(data))
