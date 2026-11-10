"""The pattern extractor through the write path (addendum §3.4.3): a library change reaches the graph
through the extraction reuse salt while the index fingerprint stays put, the generation records its
pattern provenance and library snapshot, and a library edit never breaks serving."""

from __future__ import annotations

import copy
import json

import pytest

from su_multi_arm.backends.sqlite import SqliteBackend
from su_multi_arm.core.component import Dependencies
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.types import Document
from su_multi_arm.extraction.base import BuiltinExtractor
from su_multi_arm.extraction.chunking import chunk_document
from su_multi_arm.extraction.patterns.engine import re2_available
from su_multi_arm.extraction.patterns.library import PatternLibraryError
from su_multi_arm.extraction.patterns.writer import append_rows
from su_multi_arm.ingestion.write import WritePath
from su_multi_arm.providers import Embedder
from tests.pattern_fixtures import CONNECT, FORMAT, engine_or_skip, write_library, write_system

KIND = "re2" if re2_available() else "regex"
TALK = {
    "id": "msg.talk",
    "kind": "message",
    "status": "proposed",
    "regex": r"^(?P<a>[a-z]+) (?P<t>talks to) (?P<b>[a-z]+)$",
    "signature": {"id": "talk"},
    "entities": [{"group": "a", "type": "node"}, {"group": "b", "type": "node"}],
    "relations": [{"subject": "a", "predicate": "talks_to", "object": "b", "trigger_group": "t"}],
    "examples": {"positive": ["alpha talks to beta"]},
}
LINES = "\n".join(
    [
        'ts=2026-11-04T10:00:00.000Z lvl=error comp=net.client msg="db-client failed to connect redis after 30 ms"',
        'ts=2026-11-04T10:00:01.000Z lvl=info comp=mesh.node msg="alpha talks to beta"',
    ]
)


def config(library_path):
    return validate_config(
        {
            "schema_version": 2,
            "dataset": "logs",
            "backends": {"local": {"kind": "sqlite", "path": str(library_path.parent / "state")}},
            "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
            "pipeline": {"arms": {"lexical": {"backend": "local"}, "graph": {"backend": "local"}}},
            "extraction": {"extractors": ["pattern"], "pattern_library": {"path": str(library_path), "engine": KIND}},
            "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 16}}},
        }
    )


def process(library_path):
    """A fresh process: new backend handle, new extractor (library read at build start)."""
    cfg = config(library_path)
    backend = SqliteBackend(library_path.parent / "state")
    extractor = BuiltinExtractor(cfg.extraction, Dependencies(providers={}))
    return WritePath(cfg, {"local": backend}, extractor=extractor, embedder=Embedder(cfg.models.roles.embedder)), backend


def documents():
    return [Document(id="log:1", dataset="logs", kind="unit_day", fields={"message": LINES}, metadata={})]


def facts(manifest):
    return manifest["relations"]


@pytest.fixture
def library(tmp_path):
    engine_or_skip(KIND)
    root = write_library(tmp_path / "patterns", [FORMAT, CONNECT])
    write_system(root, "proposed.yaml", [copy.deepcopy(TALK)])
    return root


def test_library_change_invalidates_extraction_reuse(library):
    path, _ = process(library)
    first = path.ingest(documents())
    fingerprint = first["fingerprint"]
    assert facts(first) == 1  # only the connect_failed fact; msg.talk is still a proposal

    path, _ = process(library)
    again = path.ingest(documents())
    assert again["reused_extractions"] == again["chunks"] and facts(again) == 1  # unchanged library: reuse hits

    append_rows(library, "decisions.yaml", [{"op": "approve", "id": "msg.talk", "by": "user:reviewer", "at": "2026-11-04T12:00:00Z"}])
    path, _ = process(library)
    approved = path.ingest(documents())
    assert approved["reused_extractions"] == 0 and facts(approved) == 2  # the new relation appears
    assert approved["fingerprint"] == fingerprint

    append_rows(library, "decisions.yaml", [{"op": "deprecate", "id": "msg.talk", "by": "user:reviewer", "at": "2026-11-04T13:00:00Z"}])
    path, _ = process(library)
    deprecated = path.ingest(documents())
    assert deprecated["reused_extractions"] == 0 and facts(deprecated) == 1  # and it is gone again
    assert deprecated["fingerprint"] == fingerprint and path.current()[1]["generation"] == deprecated["generation"]


def test_provenance_recorded(library):
    path, backend = process(library)
    manifest = path.ingest(documents())
    provenance = manifest["pattern_provenance"]
    assert set(provenance) == {"library_identity", "engine", "engine_version", "validator_version", "engine_deterministic"}
    assert provenance["engine"] == KIND and len(provenance["library_identity"]) == 64
    snapshot = backend.ledger.get_raw(manifest["pattern_library_snapshot"])
    data = json.loads(snapshot.raw_reply)
    assert data["provenance"] == provenance and [e["id"] for e in data["entries"]] == ["fmt.kv_v1", "msg.dependency.connect_failed"]


def test_library_edit_keeps_serving(library):
    path, _ = process(library)
    built = path.ingest(documents())
    append_rows(library, "decisions.yaml", [{"op": "approve", "id": "msg.talk", "by": "user:reviewer", "at": "2026-11-04T12:00:00Z"}])
    (library / "library.yaml").write_text((library / "library.yaml").read_text() + "\n# edited by hand\n")
    restarted, _ = process(library)  # restart after the edit; serving reads no library file
    gen, manifest = restarted.current()
    assert manifest["generation"] == built["generation"] and gen.fingerprint == restarted.index_fingerprint()


def test_a_broken_library_fails_the_build_before_any_write(library):
    path, backend = process(library)
    path.ingest(documents())
    before = path.current()[1]["generation"]
    append_rows(library, "decisions.yaml", [{"op": "approve", "id": "msg.unknown", "by": "user:reviewer", "at": "2026-11-04T12:00:00Z"}])
    path, _ = process(library)
    with pytest.raises(PatternLibraryError, match="unknown_decision_target"):
        path.ingest(documents())
    assert path.current()[1]["generation"] == before and path.registry.failures() == []


def test_other_extractors_keep_their_reuse_keys(tmp_path):
    cfg = config(tmp_path / "unused").model_copy(deep=True)
    cfg.extraction = cfg.extraction.model_validate({"extractors": ["gazetteer"], "gazetteer": {"node": ["alpha"]}})
    backend = SqliteBackend(tmp_path / "state")
    extractor = BuiltinExtractor(cfg.extraction, Dependencies(providers={}))
    path = WritePath(cfg, {"local": backend}, extractor=extractor, embedder=Embedder(cfg.models.roles.embedder))
    path.ingest(documents())
    recipe = extractor.recipe_fingerprint()[:16]
    chunking = cfg.ingestion.chunking
    chunk_ids = [c.id for c in chunk_document(documents()[0], chunking.size, chunking.overlap, chunking.field_order)]
    assert chunk_ids
    for chunk_id in chunk_ids:
        assert backend.ledger.get_raw(f"extraction:{recipe}:{chunk_id}") is not None  # unsalted key format
