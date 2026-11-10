"""Optional private worker cache-boundary tests; synthetic metadata, no provider calls."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

location = os.environ.get("SU_INCIDENT_PATTERN_WORKER")
if not location:
    pytest.skip("incident proposal worker not supplied", allow_module_level=True)
spec = importlib.util.spec_from_file_location("incident_proposal_worker", Path(location))
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def binding(dataset="synthetic-incidents"):
    return {"dataset": dataset, "source": "synthetic_logs", "partitions": "/synthetic/store"}


def test_inventory_binding_is_repeatable_and_prevents_cross_dataset_reuse(tmp_path):
    db = worker.database(tmp_path / "inventory.sqlite")
    worker.bind_inventory(db, binding())
    worker.bind_inventory(db, binding())
    with pytest.raises(RuntimeError, match="another dataset"):
        worker.bind_inventory(db, binding("different-synthetic"))


def test_populated_unbound_inventory_cannot_be_silently_adopted(tmp_path):
    db = worker.database(tmp_path / "inventory.sqlite")
    db.execute("INSERT INTO snapshots VALUES(?,?,?,?)", ("2026-01-01", 1, "same-library", "{}"))
    db.commit()
    with pytest.raises(RuntimeError, match="populated unbound"):
        worker.bind_inventory(db, binding())


def test_scan_identity_contains_dataset_source_and_store(tmp_path, monkeypatch):
    db = worker.database(tmp_path / "inventory.sqlite")

    class Store:
        calls = 0

        def get(self, day):
            return SimpleNamespace(version=1)

        def units(self, day, version):
            self.calls += 1
            return []

    store = Store()
    library = SimpleNamespace(identity="same-library")
    monkeypatch.setattr(worker, "INVENTORY_BINDING", binding())
    worker.scan(db, store, library, ["2026-01-01"])
    worker.scan(db, store, library, ["2026-01-01"])
    assert store.calls == 1
    monkeypatch.setattr(worker, "INVENTORY_BINDING", binding("different-synthetic"))
    worker.scan(db, store, library, ["2026-01-01"])
    assert store.calls == 2
