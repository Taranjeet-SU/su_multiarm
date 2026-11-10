"""Optional refresh checks: real file importer/store, synthetic records, stub compose.

Set SU_PARALLEL_IMPORT_DIR and SU_REFRESH_INTEGRATION_FILE to external helper
locations. SU_PARALLEL_IMPORT_CONFIG is optional; synthetic config is the default.
No source requests, model calls, serving changes or production partition writes.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from datetime import date
import fcntl

import pytest
from tests.operational.test_parallel_import import DAY, cfg_at, fixture, p

location = os.environ.get("SU_REFRESH_INTEGRATION_FILE")
if not location or not Path(location).is_file():
    pytest.skip("optional refresh integration not supplied", allow_module_level=True)
spec = importlib.util.spec_from_file_location("synthetic_refresh_candidate", Path(location))
assert spec is not None and spec.loader is not None
r = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = r
spec.loader.exec_module(r)


def args(**kw):
    return SimpleNamespace(compose_only=False, dry_run=False, import_memory_gib=24, **kw)


def setup(tmp_path, monkeypatch, ds):
    cfg = cfg_at(tmp_path / "isolated")
    monkeypatch.setattr(
        r, "Engine", lambda c: SimpleNamespace(config=c, write_path=SimpleNamespace(backends={}), backends={})
    )
    monkeypatch.setattr(r.pi, "select_units", lambda days, completed_indices: ({DAY: ds}, []))
    monkeypatch.setattr(r, "available_memory", lambda: 53 * p.GiB)
    calls = []

    def compose(self, today, library):
        calls.append((today, self.store.get(DAY)))
        return SimpleNamespace(report=lambda: {"status": "built", "generation": "synthetic-generation"})

    monkeypatch.setattr(r.pi.ParallelFileLogsPipeline, "compose", compose)
    monkeypatch.setattr(r.pi.ip.FileLogsPipeline, "compose", compose)
    return cfg, calls


def test_new_import_compose_and_pending_clear(tmp_path, monkeypatch):
    ds = [fixture(tmp_path / "raw", grouped=True)]
    cfg, calls = setup(tmp_path, monkeypatch, ds)
    out = r.run_refresh(cfg, args(), root=tmp_path, today=date(2026, 11, 5))
    assert out["imports_complete"] and out["imports"][DAY]["mapped_units"] == 1
    assert calls[0][0] == date(2026, 11, 5) and calls[0][1].status == "sealed"
    assert not (tmp_path / "state/communities/pending-selection.json").exists()


def test_low_ram_deferred_pending_preserved_and_next_run_reconsiders(tmp_path, monkeypatch):
    ds = [fixture(tmp_path / "raw", grouped=True)]
    cfg, calls = setup(tmp_path, monkeypatch, ds)
    monkeypatch.setattr(r, "available_memory", lambda: 31 * p.GiB)
    out = r.run_refresh(cfg, args(), root=tmp_path, today=date(2026, 11, 5))
    assert not out["imports_complete"] and out["imports"][DAY]["status"] == "deferred"
    assert out["imports"][DAY]["required_available_bytes"] == 32 * p.GiB
    assert calls[0][1] is None
    pending = tmp_path / "state/communities/pending-selection.json"
    saved = json.loads(pending.read_text())
    assert saved["states"][DAY]["status"] == "deferred"
    # A later completed index can join a deferred unopened day, without losing the first one.
    ds.append(fixture(tmp_path / "raw", "syntheticb", grouped=True))
    monkeypatch.setattr(r, "available_memory", lambda: 53 * p.GiB)
    out = r.run_refresh(cfg, args(), root=tmp_path, today=date(2026, 11, 5))
    assert out["imports_complete"] and out["imports"][DAY]["mapped_units"] == 2
    assert not pending.exists()


def test_oversized_day_keeps_old_sealed_and_visible_pending(tmp_path, monkeypatch):
    ds = [fixture(tmp_path / "raw")]
    cfg, calls = setup(tmp_path, monkeypatch, ds)
    r.run_refresh(cfg, args(), root=tmp_path, today=date(2026, 11, 5))
    prior = calls[-1][1]
    monkeypatch.setattr(r.pi, "estimate_memory", lambda desc, limits: 25 * p.GiB)
    out = r.run_refresh(cfg, args(), root=tmp_path, today=date(2026, 11, 5))
    assert out["imports"][DAY]["oversized_units"] == 1
    assert out["imports"][DAY]["retained_prior_version"] == prior.version
    assert calls[-1][1].version == prior.version
    assert (
        json.loads((tmp_path / "state/communities/pending-selection.json").read_text())["states"][DAY][
            "status"
        ]
        == "deferred"
    )


def test_compose_failure_keeps_frozen_selection_and_retries(tmp_path, monkeypatch):
    ds = [fixture(tmp_path / "raw")]
    cfg, calls = setup(tmp_path, monkeypatch, ds)
    good = r.pi.ParallelFileLogsPipeline.compose

    def bad(*a):
        raise RuntimeError("synthetic compose failure")

    monkeypatch.setattr(r.pi.ParallelFileLogsPipeline, "compose", bad)
    with pytest.raises(RuntimeError, match="compose failure"):
        r.run_refresh(cfg, args(), root=tmp_path, today=date(2026, 11, 5))
    pending = tmp_path / "state/communities/pending-selection.json"
    assert pending.exists()
    ds.append(fixture(tmp_path / "raw", "syntheticb"))
    monkeypatch.setattr(r.pi.ParallelFileLogsPipeline, "compose", good)
    out = r.run_refresh(cfg, args(), root=tmp_path, today=date(2026, 11, 5))
    assert out["units"][DAY] == 1 and out["imports"][DAY]["status"] == "sealed_already"
    assert not pending.exists()
    out = r.run_refresh(cfg, args(), root=tmp_path, today=date(2026, 11, 5))
    assert out["units"][DAY] == 2 and out["imports"][DAY]["cache_reused_units"] == 1


def test_legacy_pending_preserves_original_driver(tmp_path, monkeypatch):
    ds = [fixture(tmp_path / "raw")]
    cfg, calls = setup(tmp_path, monkeypatch, ds)
    pending = tmp_path / "state/communities/pending-selection.json"
    pending.parent.mkdir(parents=True)
    saved = r.pending_value(cfg, {DAY: ds}, {}, "legacy_serial")
    saved.pop("driver")
    p.atomic_json(pending, saved)
    monkeypatch.setattr(
        r.pi, "select_units", lambda *a, **kw: pytest.fail("must preserve legacy pending selection")
    )
    out = r.run_refresh(cfg, args(), root=tmp_path, today=date(2026, 11, 5))
    assert out["import_driver"] == "legacy_serial" and out["imports_complete"]
    assert not pending.exists()


def test_dryrun_does_not_construct_engine_or_pending(tmp_path, monkeypatch):
    ds = [fixture(tmp_path / "raw")]
    cfg, calls = setup(tmp_path, monkeypatch, ds)
    monkeypatch.setattr(r, "Engine", lambda cfg: pytest.fail("dry-run constructed Engine"))
    a = args()
    a.dry_run = True
    out = r.run_refresh(cfg, a, root=tmp_path, today=date(2026, 11, 5))
    assert out["dry_run"] and not (tmp_path / "state/communities/pending-selection.json").exists()


def test_pending_dataset_mismatch_and_union_loss_refuse(tmp_path, monkeypatch):
    ds = [fixture(tmp_path / "raw")]
    cfg, calls = setup(tmp_path, monkeypatch, ds)
    r.run_refresh(cfg, args(), root=tmp_path, today=date(2026, 11, 5))
    ds[:] = [fixture(tmp_path / "raw", "syntheticb")]
    out = r.run_refresh(cfg, args(), root=tmp_path, today=date(2026, 11, 5))
    assert out["imports"][DAY]["missing_prior_indices"] == 1
    assert calls[-1][1].version == 1
    pending = tmp_path / "state/communities/pending-selection.json"
    saved = json.loads(pending.read_text())
    saved["dataset"] = "other"
    p.atomic_json(pending, saved)
    with pytest.raises(RuntimeError, match="another dataset"):
        r.run_refresh(cfg, args(), root=tmp_path, today=date(2026, 11, 5))


def test_refresh_lock_direct_contention_and_inherited_owner(tmp_path, monkeypatch):
    monkeypatch.delenv("ELK_REFRESH_LOCK_FD", raising=False)
    with r.refresh_lock(tmp_path):
        with pytest.raises(BlockingIOError):
            with r.refresh_lock(tmp_path):
                pass
    with r.refresh_lock(tmp_path):
        pass
    path = tmp_path / "state/communities/refresh.lock"
    with path.open("a") as h:
        fcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = None
        try:
            try:
                previous = os.dup(9)
            except OSError:
                pass
            os.dup2(h.fileno(), 9)
            monkeypatch.setenv("ELK_REFRESH_LOCK_FD", "9")
            with r.refresh_lock(tmp_path):
                pass
            monkeypatch.delenv("ELK_REFRESH_LOCK_FD")
            with pytest.raises(BlockingIOError):
                with r.refresh_lock(tmp_path):
                    pass
        finally:
            if previous is not None:
                os.dup2(previous, 9)
                os.close(previous)
            else:
                os.close(9)
