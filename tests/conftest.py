"""Shared fixtures: the shipped v1 example config upgraded to v2 under the ``support_example``
profile (plan N12: support-domain fixtures live under that profile), the synthetic documents and an
engine on the sqlite backend in a temporary state directory."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
import yaml

from su_multi_arm import Document, Engine
from su_multi_arm.compat.config_v1 import upgrade_mapping
from su_multi_arm.core.config import validate_config

ROOT = Path(__file__).parent.parent


def support_config(state_dir: Path, profile: str = "support_example"):
    data = yaml.safe_load((ROOT / "examples/local.yaml").read_text())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        v2 = upgrade_mapping(data).data
    v2["profile"] = profile
    v2["backends"]["local"]["path"] = str(state_dir)
    return validate_config(v2)


@pytest.fixture
def config(tmp_path):
    return support_config(tmp_path / "state")


@pytest.fixture
def documents():
    path = ROOT / "examples/documents.jsonl"
    return [Document.model_validate_json(line) for line in path.read_text().splitlines()]


@pytest.fixture
def engine(config, documents):
    engine = Engine(config)
    engine.ingest(documents)
    return engine


# --- v2 synthetic fixtures (decision / selection / contract / review slices) --------------------

from su_multi_arm.backends.memory import MemoryBackend  # noqa: E402
from su_multi_arm.core.types import FieldSpec, GenerationId, MetadataSchema, Provenance, StructuredRecord  # noqa: E402
from su_multi_arm.decision import Capabilities  # noqa: E402
from su_multi_arm.runtime.context import new_context  # noqa: E402

V2_GEN = GenerationId(dataset="demo", generation="c" * 32, index_format=3, fingerprint="f" * 64)
V2_ID_PATTERN = r"\bREC-\d{3,}\b"


def v2_schema_model() -> MetadataSchema:
    return MetadataSchema(
        canonical_unit="record",
        fields=[
            FieldSpec(name="kind", type="enum", enum_values=["note", "record", "report"], nullable=False, groupable=True, listable=True),
            FieldSpec(name="project", type="string", listable=True, groupable=True, casefold_match=True),
            FieldSpec(name="status", type="string", listable=True),
            FieldSpec(name="external_id", type="id", listable=False, max_catalog_cardinality=0),
            FieldSpec(name="tag", type="string", listable=True, max_catalog_cardinality=2),
            FieldSpec(name="amount", type="number", sortable=True),
            FieldSpec(name="priority", type="integer"),
            FieldSpec(name="archived", type="boolean"),
            FieldSpec(name="created_at", type="datetime", time_basis="created", sortable=True),
            FieldSpec(name="updated_at", type="datetime", time_basis="updated", timezone_policy="assume_utc"),
            FieldSpec(name="resolved_on", type="date", time_basis="resolved"),
            FieldSpec(name="region", type="string", filterable=False),
        ],
    )


def v2_records() -> list[StructuredRecord]:
    rows = [
        ("r1", "record", "Alpha", "open", "REC-1001", "t1", 10.5, 1, False, "2024-03-02T10:00:00+00:00", "2024-03-05", "north"),
        ("r2", "record", "Alpha", "closed", "REC-1002", "t2", 20.0, 2, True, "2024-03-20T10:00:00+00:00", "2024-04-01", "south"),
        ("r3", "note", "Beta", "open", "REC-1003", "t3", 5.0, 3, False, "2024-04-01T00:00:00+00:00", None, "north"),
        ("r4", "report", "Beta", "open", "REC-1004", "t1", None, 2, False, "2023-12-31T23:30:00-01:00", "2024-01-15", "south"),
        ("r5", "record", "Gamma", "pending", "REC-1005", "t2", 7.25, 1, False, "2024-03-31T23:00:00+00:00", "2024-03-31", "north"),
    ]
    records = []
    for rid, kind, project, status, ext, tag, amount, priority, archived, created, resolved, region in rows:
        values = {
            "kind": kind,
            "project": project,
            "status": status,
            "external_id": ext,
            "tag": tag,
            "amount": amount,
            "priority": priority,
            "archived": archived,
            "created_at": created,
            "resolved_on": resolved,
            "region": region,
        }
        records.append(StructuredRecord(schema_id="demo", id=rid, values=values, provenance=Provenance(extractor="fixture")))
    return records


@pytest.fixture
def v2_schema():
    return v2_schema_model()


@pytest.fixture
def v2_backend(v2_schema):
    backend = MemoryBackend()
    backend.records.upsert(V2_GEN, v2_records(), v2_schema)
    return backend


@pytest.fixture
def v2_gen():
    return V2_GEN


@pytest.fixture
def v2_ctx():
    return new_context(V2_GEN)


@pytest.fixture
def v2_caps(v2_schema):
    return Capabilities(
        arms=["dense", "lexical", "structured", "graph", "community"],
        metadata_schema=v2_schema,
        id_patterns=[V2_ID_PATTERN],
    )


# Modules that import an optional extra at module level are skipped, not errored, when the extra is
# not installed (a fresh ``pip install su-multi-arm[dev]`` has no ``api`` extra).
import importlib.util as _importlib_util  # noqa: E402

collect_ignore: list[str] = []
if _importlib_util.find_spec("fastapi") is None:
    collect_ignore += [
        "contract/test_serving_http.py",
        "parity/test_request_parity.py",
        "parity/test_serving_parity.py",
        "test_interfaces.py",
    ]
