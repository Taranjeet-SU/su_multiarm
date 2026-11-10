"""Frozen member, slice, layer and view types (unified merge plan S0, §6.1, §6.4, §6.7).

The F0 fixture (``tests/fixtures/unified``) is mapped onto the types: every member instance is a
``MemberRef``, every ledger entry a ``SliceRef`` (the gap-filling day rebuild carries its
tombstones), the current ledger is a valid ``PinnedView``, and the F0 traps that are type-level
violations are refused. The protocols are checked with small in-memory fakes.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from su_multi_arm.core.types_members import (
    DEFAULT_CATALOG_KINDS,
    FacetLocation,
    IdDirectory,
    IdDirectoryEntry,
    LayerRef,
    MemberRef,
    PartitionRecipe,
    PinnedView,
    SliceLedger,
    SliceRef,
    StratumRanking,
    StratumScorer,
    canonical_sha256,
    is_catalog_kind,
)
from su_multi_arm.testing.fixtures.unified import load

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "unified"
SHA = "c" * 64
NOW = datetime(2026, 11, 9, 10, 30, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def fx():
    return load(ROOT)


def recipe(fx_member: dict | None = None) -> PartitionRecipe:
    spec = (fx_member or {}).get("recipe", {})
    return PartitionRecipe(
        embedding_space={"id": "fixture-space-1", "model": "hash-demo-v1", "dimensions": 256},
        analyzer=spec.get("analyzer", {}),
        chunking=spec.get("chunking", {}),
        content=spec.get("content", {}),
    )


def member_ref(fx, m: dict) -> MemberRef:
    documents = fx.members[f"{m['id']}/{m['key']}"]
    partition = canonical_sha256([[d.id, d.model_dump(mode="json")] for d in documents] + [recipe(m).fingerprint()])
    return MemberRef(member=m["id"], key=m["key"], partition_id=partition, recipe=recipe(m), tier=m["tier"], as_of=NOW, sources=tuple(m["sources"]), counts=m["counts"])


def slice_ref(entry: dict, day: str, sequence: int) -> SliceRef:
    return SliceRef(
        slice_id=entry["slice_id"], member="logs", source="logs", day=date.fromisoformat(day), level=entry["level"], sequence=sequence,
        as_of=datetime.fromisoformat(entry["as_of"].replace("Z", "+00:00")), recipe=recipe(), keyword_epoch=entry["keyword_epoch"],
        documents=entry["documents"], replaces=tuple(entry.get("replaces", ())), supersedes=tuple(s["id"] for s in entry.get("supersedes", ())),
        tombstones=tuple(entry.get("retires", ())), sha256=entry["sha256"],
    )


def all_slices(fx) -> dict[str, SliceRef]:
    return {e["slice_id"]: slice_ref(e, day, i) for day, entries in fx.ledger["days"].items() for i, e in enumerate(entries)}


# --- the fixture maps onto the types ------------------------------------------------------------------


def test_every_fixture_member_is_a_member_ref(fx):
    refs = [member_ref(fx, m) for m in fx.manifest["members"]]
    assert {r.unit_id for r in refs} >= {"support/adopted:a7fc28ae68e9cf51", "logs/month:2026-10", "logs/day:2026-11-06"}
    assert len({r.partition_id for r in refs}) == len(refs)
    month = next(r for r in refs if r.key == "month:2026-10")
    assert month.covers(date(2026, 10, 31)) and not month.covers(date(2026, 11, 1))


def test_every_ledger_entry_is_a_slice_ref_and_the_gap_rebuild_carries_tombstones(fx):
    slices = all_slices(fx)
    rebuild = slices["slice:2026-11-07:day"]
    assert rebuild.level == "day" and set(rebuild.replaces) == {"slice:2026-11-07:base", "slice:2026-11-07:h13"}
    assert set(rebuild.tombstones) == set(fx.manifest["expected"]["gap_merge"]["tombstones"]) and rebuild.tombstones
    assert slices["slice:2026-11-09:m1025"].supersedes


def test_the_current_ledger_is_a_valid_pinned_view(fx):
    members = tuple(member_ref(fx, m) for m in fx.manifest["members"])
    vector = {"logs": {date.fromisoformat(d): tuple(ids) for d, ids in fx.ledger["current"].items()}}
    view = PinnedView(generation="g" * 32, members=members, slices=vector)
    assert view.unit_ids()[-1] == "slice:2026-11-09:m1025"
    assert {m.member for m in view.members_for(["ticket"])} == {"support"}
    assert {m.member for m in view.members_for(["logs"])} == {"logs"}


def test_the_archived_day_slice_trap_is_refused(fx):
    """F0 trap ``archived_day_slice``: a day is in exactly one tier (archive wins)."""
    members = tuple(member_ref(fx, m) for m in fx.manifest["members"])
    with pytest.raises(ValidationError, match="exactly one tier"):
        PinnedView(members=members, slices={"logs": {date(2026, 11, 6): ("slice:2026-11-06:late",)}})
    with pytest.raises(ValidationError, match="exactly one tier"):  # a month member covers its days
        PinnedView(members=members, slices={"logs": {date(2026, 10, 3): ("slice:2026-10-03:late",)}})
    # Another member's archived day does not collide with the log member's slices.
    assert PinnedView(members=members, slices={"tracker": {date(2026, 11, 6): ("slice:2026-11-06:t",)}})


# --- frozen and strict --------------------------------------------------------------------------------


def test_types_are_frozen_and_strict(fx):
    ref = member_ref(fx, fx.manifest["members"][0])
    with pytest.raises(ValidationError):
        ref.tier = "snapshot"
    with pytest.raises(ValidationError, match="Extra inputs"):
        MemberRef.model_validate({**ref.model_dump(), "extra": 1})
    assert MemberRef.model_validate(ref.model_dump()) == ref


@pytest.mark.parametrize("key", ["adopted:XYZ", "snapshot:2026-11", "day:2026-1-1", "month:2026-10-01", "week:2026-45", "catalog:logs"])
def test_member_keys_follow_the_declared_forms(fx, key):
    ref = member_ref(fx, fx.manifest["members"][1]).model_dump()
    with pytest.raises(ValidationError):
        MemberRef.model_validate({**ref, "key": key})


def test_catalog_members_hold_catalog_kinds_only_there(fx):
    base = member_ref(fx, fx.manifest["members"][2]).model_dump()
    catalog = MemberRef.model_validate({**base, "key": "catalog", "tier": "catalog", "catalog_kinds": ("signature",)})
    assert catalog.day() is None and not catalog.covers(date(2026, 11, 4))
    with pytest.raises(ValidationError, match="catalog kinds"):
        MemberRef.model_validate({**base, "key": "catalog", "tier": "catalog"})
    with pytest.raises(ValidationError, match="only a catalog member"):
        MemberRef.model_validate({**base, "catalog_kinds": ("signature",)})
    with pytest.raises(ValidationError, match="key catalog"):
        MemberRef.model_validate({**base, "tier": "catalog", "catalog_kinds": ("signature",)})
    with pytest.raises(ValidationError, match="adopted key"):
        MemberRef.model_validate({**base, "tier": "adopted"})
    assert DEFAULT_CATALOG_KINDS == ("signature",) and is_catalog_kind("signature") and is_catalog_kind("rule", ["rule"]) and not is_catalog_kind("incident")


def test_digests_and_facets_are_checked(fx):
    base = member_ref(fx, fx.manifest["members"][1]).model_dump()
    ok = MemberRef.model_validate({**base, "digests": {"catalog.sqlite": SHA}, "facets": {"catalog": {"backend": "sqlite", "location": "members/tracker/catalog.sqlite"}}})
    assert ok.facets["catalog"].read_only
    with pytest.raises(ValidationError, match="sha256"):
        MemberRef.model_validate({**base, "digests": {"catalog.sqlite": "abc"}})
    for location in ("/srv/store", "~/store", "../escape", "C:/store"):
        with pytest.raises(ValidationError, match="relative"):
            FacetLocation(backend="sqlite", location=location)


def test_slice_rules(fx):
    entry = all_slices(fx)["slice:2026-11-07:day"].model_dump()
    with pytest.raises(ValidationError, match="day"):
        SliceRef.model_validate({**entry, "day": date(2026, 11, 8)})
    with pytest.raises(ValidationError, match="replace itself"):
        SliceRef.model_validate({**entry, "replaces": ("slice:2026-11-07:day",)})
    with pytest.raises(ValidationError, match="superseded or tombstoned"):
        SliceRef.model_validate({**entry, "supersedes": entry["tombstones"][:1]})
    with pytest.raises(ValidationError, match="unique"):
        SliceRef.model_validate({**entry, "tombstones": entry["tombstones"][:1] * 2})
    with pytest.raises(ValidationError):
        SliceRef.model_validate({**entry, "level": "weekly"})


def test_id_directory_entries():
    row = IdDirectoryEntry(doc_id="logs:incident:1", kind="incident", unit_id="slice:2026-11-07:h13", unit_kind="slice", day=date(2026, 11, 7))
    assert row.live
    assert not row.model_copy(update={"deleted_by": "slice:2026-11-07:day"}).live
    with pytest.raises(ValidationError, match="not both"):
        IdDirectoryEntry(doc_id="x", kind="incident", unit_id="u", unit_kind="slice", superseded_by="a", deleted_by="b")
    with pytest.raises(ValidationError, match="live in slices"):
        IdDirectoryEntry(doc_id="x", kind="signature", unit_id="logs/catalog", unit_kind="member", catalog_provisional=True)


def test_view_rules(fx):
    ref = member_ref(fx, fx.manifest["members"][0])
    with pytest.raises(ValidationError, match="twice"):
        PinnedView(members=(ref, ref))
    with pytest.raises(ValidationError, match="pinned twice"):
        PinnedView(slices={"logs": {date(2026, 11, 8): ("slice:2026-11-08:a",)}, "other": {date(2026, 11, 8): ("slice:2026-11-08:a",)}})
    with pytest.raises(ValidationError, match="not a slice of"):
        PinnedView(slices={"logs": {date(2026, 11, 8): ("slice:2026-11-09:a",)}})
    layer = LayerRef(name="graph", kind="graph_attach", sources=("ticket", "doc"), arms=("graph",), digest=SHA)
    with pytest.raises(ValidationError, match="layer appears twice"):
        PinnedView(layers=(layer, layer))


def test_the_partition_recipe_fingerprint_is_canonical_and_sensitive():
    a = recipe()
    assert a.fingerprint() == PartitionRecipe.model_validate(a.model_dump()).fingerprint()
    assert a.fingerprint() != a.model_copy(update={"content": {"keyword_epoch": "kw-e2"}}).fingerprint()
    with pytest.raises(ValidationError):
        PartitionRecipe(analyzer={"engine": "lucene"})


def test_stratum_rankings_are_ordered_and_aligned():
    assert StratumRanking(stratum="support", ids=("a", "b"), scores=(2.0, 1.0)).ids == ("a", "b")
    with pytest.raises(ValidationError, match="descending"):
        StratumRanking(stratum="support", ids=("a", "b"), scores=(1.0, 2.0))
    with pytest.raises(ValidationError, match="aligned"):
        StratumRanking(stratum="support", ids=("a",), scores=())


# --- protocols ------------------------------------------------------------------------------------------


class FakeDirectory:
    def __init__(self):
        self.rows: dict[str, IdDirectoryEntry] = {}

    def register(self, unit_id, unit_kind, entries):
        for e in entries:
            if e.doc_id in self.rows and self.rows[e.doc_id].live:
                raise ValueError(f"duplicate id {e.doc_id}")
            self.rows[e.doc_id] = e

    def supersede(self, doc_ids, by):
        for d in doc_ids:
            self.rows[d] = self.rows[d].model_copy(update={"superseded_by": by})

    def tombstone(self, doc_ids, by):
        for d in doc_ids:
            self.rows[d] = self.rows[d].model_copy(update={"deleted_by": by})

    def retire(self, unit_id):
        self.rows = {k: v for k, v in self.rows.items() if v.unit_id != unit_id}

    def resolve(self, doc_id, view):
        row = self.rows.get(doc_id)
        return row if row is not None and row.live and row.unit_id in view.unit_ids() else None

    def live_ids(self, member, day):
        return frozenset(k for k, v in self.rows.items() if v.live and v.day == day)


class FakeLedger:
    def vector(self):
        return {}

    def get(self, slice_id):
        raise KeyError(slice_id)

    def seal(self, day, level, documents, *, replaces=()):
        raise NotImplementedError

    def retire(self, slice_id):
        return False


class FakeScorer:
    stratum = "support"

    def rank(self, query, k, view):
        return StratumRanking(stratum=self.stratum)


def test_protocols_are_runtime_checkable_and_a_tombstoned_id_is_never_resolved(fx):
    directory = FakeDirectory()
    assert isinstance(directory, IdDirectory) and isinstance(FakeLedger(), SliceLedger) and isinstance(FakeScorer(), StratumScorer)
    slices = all_slices(fx)
    rebuild = slices["slice:2026-11-07:day"]
    gone = rebuild.tombstones[0]
    directory.register("slice:2026-11-07:h13", "slice", [IdDirectoryEntry(doc_id=gone, kind="incident", unit_id="slice:2026-11-07:h13", unit_kind="slice", day=rebuild.day)])
    view = PinnedView(slices={"logs": {rebuild.day: ("slice:2026-11-07:h13",)}})
    assert directory.resolve(gone, view) is not None
    directory.tombstone(rebuild.tombstones[:1], rebuild.slice_id)
    assert directory.resolve(gone, view) is None and gone not in directory.live_ids("logs", rebuild.day)
