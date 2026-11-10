"""Delta reparse and the partition format gate (addendum A2 §4.1.6, reviews B3/B6, rev 3 R2-B3).

* ``library_diff`` classes A–H, and a seeded randomised property test: for random library changes the
  delta reparse yields exactly the full reparse (matches and aggregates);
* a library change on day 11 (one new entry) keeps ≥ 95 % of the day documents by id and content;
* a format-1 partition in the window is reparsed before compose (mixed-format window);
* the per-day derived snapshot carries the typed-value aggregates (round trip).
Synthetic gold set; evidence level: contract.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random

import pytest
import yaml

from su_multi_arm.ingestion.logs_compose import read_derived
from su_multi_arm.ingestion.reparse import library_diff, library_digest, plan_delta
from su_multi_arm.testing.fixtures.logs.gold import LIBRARY, day_of

from tests.logs_gold_support import AS_OF, SPEC, build_gold

DAYS = [day_of(SPEC, 8), day_of(SPEC, 9)]


def _entry(entry_id: str, signature: str, regex: str, example: str, priority: int = 70, kind: str = "message") -> dict:
    data = {"id": entry_id, "kind": kind, "status": "approved", "priority": priority, "regex": regex, "examples": {"positive": [example]}, "provenance": {"author": "user"}}
    if kind == "message":
        data["signature"] = {"id": signature, "category": "test"}
    return data


MUTATIONS = {
    "steal_crash": lambda lib: lib["patterns"].append(_entry("msg.oom", "process.oom", r"process crashed: (?P<why>out of memory)", "process crashed: out of memory", 80)),
    "parse_opaque": lambda lib: lib["patterns"].append(_entry("msg.opaque", "payload.opaque", r"opaque payload marker (?P<n>[0-9]{1,6})", "opaque payload marker 81")),
    "retire_flag": lambda lib: lib.__setitem__("patterns", [p for p in lib["patterns"] if p["id"] != "msg.feature.flag_conflict"]),
    "edit_crash": lambda lib: next(p for p in lib["patterns"] if p["id"] == "msg.process.crashed")["signature"].__setitem__("category", "crash"),
    "annotate_5xx": lambda lib: lib["patterns"].append(
        {"id": "ann.server_error", "kind": "annotate", "status": "approved", "priority": 5, "regex": r"status=(?P<code>5[0-9]{2})", "entities": [{"group": "code", "type": "status"}], "examples": {"positive": ["GET /x status=503 latency_ms=1"]}, "provenance": {"author": "user"}}
    ),
    "edit_noise": lambda lib: next(p for p in lib["patterns"] if p["id"] == "noise.healthz").__setitem__("regex", r"^GET /healthz 200 [0-9]{1,4}ms$"),
}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("delta")
    engine, pipe, report = build_gold(tmp, days=DAYS)
    return engine, pipe, tmp


def _matches(parsed) -> list[dict]:
    return sorted((json.dumps({k: v for k, v in m.items() if k != "unit_id"}, sort_keys=True) for m in parsed.matches))


def _library(pipe, tmp, names: list[str]):
    data = copy.deepcopy(LIBRARY)
    for name in names:
        MUTATIONS[name](data)
    (tmp / "patterns" / "library.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    try:
        return pipe.library()
    finally:
        (tmp / "patterns" / "library.yaml").write_text(yaml.safe_dump(LIBRARY, sort_keys=False))


def test_change_classes():
    base = {"fmt": {"kind": "format", "sha": "1"}, "m": {"kind": "message", "sha": "1"}, "a": {"kind": "annotate", "sha": "1", "regex": "x"}, "__engine__": {"kind": "engine", "sha": "e"}}
    new = {**base, "fmt": {"kind": "format", "sha": "2"}, "m2": {"kind": "message", "sha": "1"}, "a": {"kind": "annotate", "sha": "2", "regex": "y"}, "__engine__": {"kind": "engine", "sha": "f"}}
    new.pop("m")
    classes = {(c.cls, c.entry) for c in library_diff(base, new, redaction_changed=True)}
    assert classes == {("A", "fmt"), ("C", "a"), ("E", "m2"), ("F", "m"), ("G", "redaction"), ("H", "__engine__")}
    edited = {**base, "m": {"kind": "message", "sha": "9"}}
    assert [(c.cls, c.entry) for c in library_diff(base, edited)] == [("D", "m")]


@pytest.mark.parametrize("seed", range(12))
def test_delta_equals_full_for_random_library_changes(built, seed):
    engine, pipe, tmp = built
    names = random.Random(seed).sample(sorted(MUTATIONS), random.Random(seed + 100).randint(1, 3))
    library = _library(pipe, tmp, names)
    key = DAYS[1].isoformat()
    store = pipe.store
    old = store.read(key, "patterns.json")
    matches = store.read(key, "matches.jsonl.gz")
    units = list(store.units(key, store.get(key).version))
    heads = {r.id: r.message.split("\n", 1)[0] for u in units for r in u.records}
    plan = plan_delta(old, library, matches, heads, max_delta_share=1.0)
    assert plan.mode == "delta", plan.report()
    reuse = {m["id"]: m for m in matches if m["id"] not in plan.delta}
    delta = pipe.parse(units, library, reuse=reuse)
    full = pipe.parse(units, library)
    assert _matches(delta) == _matches(full), names
    assert delta.aggregates == full.aggregates and delta.hourly_cells == full.hourly_cells, names
    assert len(plan.delta) < len(matches), names  # a delta, not everything


def test_format_change_is_a_full_reparse(built):
    engine, pipe, tmp = built
    data = copy.deepcopy(LIBRARY)
    data["patterns"][0]["priority"] = 101
    (tmp / "patterns" / "library.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    try:
        library = pipe.library()
    finally:
        (tmp / "patterns" / "library.yaml").write_text(yaml.safe_dump(LIBRARY, sort_keys=False))
    key = DAYS[1].isoformat()
    plan = plan_delta(pipe.store.read(key, "patterns.json"), library, pipe.store.read(key, "matches.jsonl.gz"), {})
    assert plan.mode == "full" and "A" in plan.classes


def _doc_hashes(engine) -> dict[str, str]:
    corpus = engine.write_path._load_corpus(engine.write_path.current()[0])
    return {d.id: hashlib.sha256(json.dumps([d.fields, d.metadata], sort_keys=True).encode()).hexdigest() for d in corpus.values() if "date" in d.metadata}


def test_day_eleven_library_change_reuses_day_documents(tmp_path):
    engine, pipe, _ = build_gold(tmp_path)
    before = _doc_hashes(engine)
    data = copy.deepcopy(LIBRARY)
    MUTATIONS["steal_crash"](data)
    (tmp_path / "patterns" / "library.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    library = pipe.library()
    result = pipe.compose(AS_OF, library)
    modes = {k: v["mode"] for k, v in result.reparse.items()}
    assert modes and set(modes.values()) == {"delta"}
    after = _doc_hashes(engine)
    kept = sum(1 for doc_id, digest in before.items() if after.get(doc_id) == digest)
    assert kept / len(before) >= 0.95, (kept, len(before))
    out = engine.call_tool("log_top", {"by": "signature", "level": ["fatal"], "window": "yesterday", "as_of": AS_OF.isoformat()}).result.output
    assert [(r["signature_id"], r["lines"]) for r in out.rows] == [("process.oom", 4)]


def test_mixed_format_window_reparses_the_old_partition(tmp_path):
    engine, pipe, _ = build_gold(tmp_path, days=DAYS)
    key = DAYS[0].isoformat()
    manifest = pipe.store.get(key)
    path = pipe.store._vdir(key, manifest.version) / "manifest.json"
    path.write_text(manifest.model_copy(update={"format": 1}).model_dump_json(indent=2))
    data = copy.deepcopy(LIBRARY)  # any new composition (the active one would be unchanged)
    data["patterns"].append(_entry("msg.unused", "unused.never", r"never matches this line zq", "never matches this line zq"))
    (tmp_path / "patterns" / "library.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    result = pipe.compose(AS_OF, pipe.library())
    assert result.reparse[key]["mode"] == "full" and "format 1" in result.reparse[key]["reason"]
    assert pipe.store.get(key).format == 2


def test_derived_snapshot_round_trips_the_typed_aggregates(built):
    engine, pipe, _ = built
    key = DAYS[0].isoformat()
    snapshot = read_derived(pipe.store, key)
    sealed = pipe.store.read(key, "aggregates.json")
    for name in ("levels", "level_hours", "endpoints", "exceptions"):
        assert snapshot["aggregates"][name] == sealed[name] and snapshot["aggregates"][name]
    assert snapshot["aggregates"]["format"] == 2
    assert all("deployment" in c and "row_exactness" in c for c in snapshot["hourly_cells"])
    assert library_digest(pipe.library()).keys() == pipe.store.read(key, "patterns.json").keys()
