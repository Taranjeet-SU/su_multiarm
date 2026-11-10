"""Synthetic optional harness for the externally supplied private export wrapper.

Set SU_GROUPED_WRAPPER_DIR to its directory. No real transport, deployment paths,
credentials or source data are used; all runtime dependencies are injected stubs.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def wrapper(tmp_path, monkeypatch):
    location = os.environ.get("SU_GROUPED_WRAPPER_DIR")
    if not location:
        pytest.skip("private grouped wrapper not supplied")
    path = Path(location) / "grouped_pull.py"
    assert path.is_file()
    pytest.importorskip("zstandard")
    def module(name, **attrs):
        result = ModuleType(name)
        result.__file__ = str(tmp_path / (name + ".py"))
        Path(result.__file__).write_text("# Synthetic dependency\n")
        result.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, result)
        return result
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    module("grouped_esro", ROOT=tmp_path, strata_queries=lambda _: {}, ReadOnlyES=lambda *a, **k: pytest.fail("unexpected source connection"))
    redactor = module("synthetic_redactor")
    module("full_pull", digest=digest, redact=redactor, record=lambda hit, info, rc, safe: hit)
    def atomic(path, value):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value))
        tmp.replace(path)
    module("pull_day", atomic_json=atomic)
    module("indexnames", parse=lambda _: None)
    module("pull_widen", Pacer=None)
    from scripts import grouped_log_capture
    monkeypatch.setitem(sys.modules, "grouped_log_capture", grouped_log_capture)
    spec = importlib.util.spec_from_file_location("synthetic_grouped_wrapper", path)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.shutil, "disk_usage", lambda _: SimpleNamespace(free=200 * (1 << 30)))
    return mod


INFO = {"index": "synthetic-demo-prod-service-2026.01.01", "date": "2026-01-01", "tenant": "demo", "env": "prod", "service": "service"}
RESULT = {"contract": "elk_grouped_v1", "source_total": 5, "grouped_occurrences": 4, "raw_occurrences": 1,
          "groups": 1, "downloaded_examples": 1, "downloaded_documents": 2, "es_counts": {"total": 5},
          "snapshot_consistent": True, "snapshot": {"protocol": "pit", "snapshot_consistent": True}, "capped": False}


def build(wrapper, tmp_path):
    es = SimpleNamespace(request=lambda *a, **kw: {INFO["index"]: {"mappings": {"properties": {}}}})
    return wrapper.GroupedPuller(es, INFO, 100, SimpleNamespace(stamp="synthetic"), lambda _: None, base_root=tmp_path / "exports")


def test_atomic_seal_reuse_and_corruption_retry(wrapper, tmp_path, monkeypatch):
    calls = []
    def capture(es, info, props, emit, sanitize, **kwargs):
        calls.append(True)
        emit({"kind": "group", "examples": [{"id": "synthetic-a", "message": "[MASKED]"}]})
        emit({"kind": "raw", "record": {"id": "synthetic-b", "message": "[MASKED]"}})
        return dict(RESULT)
    monkeypatch.setattr(wrapper, "capture_index", capture)
    # Recipe introspection needs a module-backed callable.
    capture.__module__ = "grouped_log_capture"
    puller = build(wrapper, tmp_path)
    pacer = SimpleNamespace(boundary=lambda: None)
    puller.run([INFO], pacer)
    manifest = json.loads(puller.mpath.read_text())
    assert manifest["summary"]["sealed"] is True
    entry = manifest["indices"][INFO["index"]]
    assert entry["snapshot_consistent"] is True
    path = puller.base / "raw" / entry["file"]
    assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert entry["source_total"] == 5 and entry["downloaded_documents"] == 2
    puller.run([INFO], pacer)
    assert len(calls) == 1
    path.write_bytes(b"synthetic corruption")
    puller.run([INFO], pacer)
    assert len(calls) == 2
    assert not list(path.parent.glob("*.part"))


def test_failed_capture_retains_only_masked_partial_and_unsealed_manifest(wrapper, tmp_path, monkeypatch):
    def capture(es, info, props, emit, sanitize, **kwargs):
        emit({"kind": "raw", "record": {"message": "[MASKED]"}})
        raise RuntimeError("synthetic source failure")
    capture.__module__ = "grouped_log_capture"
    monkeypatch.setattr(wrapper, "capture_index", capture)
    puller = build(wrapper, tmp_path)
    with pytest.raises(RuntimeError, match="synthetic source failure"):
        puller.run([INFO], SimpleNamespace(boundary=lambda: None))
    manifest = json.loads(puller.mpath.read_text())
    assert manifest["summary"]["sealed"] is False
    assert manifest["indices"][INFO["index"]]["status"] == "failed"
    assert len(list((puller.base / "raw").glob("*.part"))) == 1
    assert not list((puller.base / "raw").glob("*.jsonl.zst"))


def test_explicit_unverified_cli_refuses_before_connection(wrapper, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["grouped_pull.py", "--days", "2026-01-01", "--snapshot-protocol", "unverified"])
    with pytest.raises(SystemExit) as err:
        wrapper.main()
    assert err.value.code == 2


def test_adaptive_wrapper_selects_compatible_capture_and_changes_recipe(wrapper, tmp_path, monkeypatch):
    calls = []
    def capture(es, info, props, emit, sanitize, **kwargs):
        calls.append('compatible')
        emit({'kind': 'group', 'examples': [{'id': 'one'}]})
        emit({'kind': 'raw', 'record': {'id': 'two'}})
        return {**RESULT, 'snapshot': {'protocol': 'single_search', 'snapshot_consistent': True}}
    monkeypatch.setattr(wrapper, 'capture_index_compatible', capture)
    old = build(wrapper, tmp_path)
    adaptive = wrapper.GroupedPuller(old.es, INFO, 100, SimpleNamespace(stamp='synthetic'), lambda _: None,
                                     base_root=tmp_path / 'adaptive', protocol='adaptive')
    assert old.recipe != adaptive.recipe
    adaptive.run([INFO], SimpleNamespace(boundary=lambda: None))
    assert calls == ['compatible']
    assert adaptive.manifest['summary']['sealed'] is True


def test_completed_full_index_is_hardlinked_without_capture_even_before_service_seal(wrapper, tmp_path, monkeypatch):
    base = wrapper.ROOT / 'data/full/partitions' / INFO['date'] / INFO['service']
    (base / 'raw').mkdir(parents=True)
    source = base / 'raw' / 'synthetic.jsonl.zst'
    source.write_bytes(b'Synthetic already-masked export')
    info = {'status': 'done', 'full_capture': True, 'capped': False, 'finished': '2026-01-02T00:00:00Z',
            'es_counts': {'F': 0, 'E': 3, 'W': 0, 'I': 0, 'total': 3}, 'pulled': {'A': 3},
            'tenant': INFO['tenant'], 'env': INFO['env'], 'service': INFO['service'],
            'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'file': source.name}
    (base / 'manifest.json').write_text(json.dumps({'summary': {'sealed': False}, 'indices': {INFO['index']: info}}))
    puller = build(wrapper, tmp_path)
    monkeypatch.setattr(puller, 'pull_index', lambda *_: pytest.fail('completed source was downloaded again'))
    puller.run([INFO], SimpleNamespace(boundary=lambda: None))
    row = puller.manifest['indices'][INFO['index']]
    assert row['storage_format'] == 'full_records_v1'
    assert row['newly_downloaded_documents'] == 0 and row['reused_documents'] == 3
    assert row['snapshot']['protocol'] == 'reused_full_scroll'
    assert (puller.base / 'raw' / row['file']).stat().st_ino == source.stat().st_ino
    assert puller.manifest['summary']['sealed'] is True


def test_prior_raw_recipe_migration_requires_explicit_allowlist_and_full_proof(wrapper, tmp_path, monkeypatch):
    puller = build(wrapper, tmp_path)
    path = puller.base / 'raw' / 'synthetic.jsonl.zst'
    path.write_bytes(b'Synthetic exact masked raw export')
    prior = 'a' * 64
    row = {'recipe': prior, 'status': 'done', 'contract': 'elk_grouped_v1',
           'capture_mode': 'raw_scroll', 'snapshot_consistent': True, 'capped': False,
           'snapshot': {'protocol': 'scroll', 'snapshot_consistent': True},
           'source_total': 3, 'raw_occurrences': 3, 'downloaded_documents': 3,
           'groups': 0, 'grouped_occurrences': 0, 'downloaded_examples': 0,
           'es_counts': {'F': 0, 'E': 3, 'W': 0, 'I': 0, 'total': 3},
           'tenant': INFO['tenant'], 'env': INFO['env'], 'service': INFO['service'],
           'includes': wrapper.SOURCE_FIELDS, 'file': path.name, 'bytes': path.stat().st_size,
           'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'finished': '2026-01-02T00:00:00Z'}
    assert puller.reuse_prior_raw(INFO, row) is None
    puller.trusted_raw_recipes = frozenset([prior])
    migrated = puller.reuse_prior_raw(INFO, row)
    assert migrated['newly_downloaded_documents'] == 0 and migrated['reused_documents'] == 3
    assert migrated['migrated_from_recipe'] == prior
    assert migrated['recipe'] == puller.recipe
    assert puller.reuse_prior_raw(INFO, {**row, 'capture_mode': 'single_response_groups'}) is None
    for field, value in [('env', 'other'), ('raw_occurrences', 2), ('sha256', 'bad'), ('includes', []), ('bytes', 0)]:
        with pytest.raises(RuntimeError):
            puller.reuse_prior_raw(INFO, {**row, field: value})
    puller.manifest['indices'][INFO['index']] = row
    monkeypatch.setattr(puller, 'pull_index', lambda *_: pytest.fail('approved raw export was downloaded again'))
    puller.run([INFO], SimpleNamespace(boundary=lambda: None))
    assert puller.manifest['summary']['sealed'] is True
    assert puller.manifest['summary']['newly_downloaded_documents'] == 0


def test_hybrid_requires_head_policy_and_hashes_policy_identity(wrapper, tmp_path):
    old = build(wrapper, tmp_path)
    common = dict(base_root=tmp_path / 'hybrid', protocol='adaptive', hybrid_candidates=16)
    with pytest.raises(ValueError, match='masked-head policy'):
        wrapper.GroupedPuller(old.es, INFO, 100, SimpleNamespace(stamp='synthetic'), lambda _: None, **common)
    one = wrapper.GroupedPuller(old.es, INFO, 100, SimpleNamespace(stamp='synthetic'), lambda _: None,
                               head_policy=(lambda text, info: True, 'policy-one'), **common)
    two = wrapper.GroupedPuller(old.es, INFO, 100, SimpleNamespace(stamp='synthetic'), lambda _: None,
                               head_policy=(lambda text, info: True, 'policy-two'), **common)
    assert one.recipe != two.recipe


def test_real_head_policy_preserves_meaningful_continuation_evidence(wrapper, tmp_path, monkeypatch):
    from su_multi_arm.core.config import validate_config
    from tests.logs_support import config_data, write_library
    write_library(tmp_path / 'patterns')
    data = config_data(tmp_path)
    data['sources']['synthetic']['multiline'] = {'enabled': True, 'continuation': [
        'indented', 'java_frame', 'python_frame', 'traceback_header', 'python_traceback',
        'caused_by', 'ellipsis_more', 'closing_bracket']}
    cfg = validate_config(data)
    safe = SimpleNamespace(stamp='synthetic-privacy', redact=lambda text: (text, {}))
    monkeypatch.setattr(wrapper.full_pull, 'record', lambda hit, info, rc, safe: {
        'id': hit['_id'], 'ts': hit['_source']['@timestamp'], 'message': hit['_source']['message'],
        'env': info['env']})
    eligible, identity, raw_context = wrapper.make_head_policy(cfg, safe)
    def record(text):
        return {'id': 'synthetic', 'ts': '2026-01-01T00:00:00Z', 'message': text, 'env': INFO['env']}
    assert len(identity) == 64
    assert eligible('synthetic operation completed', INFO)
    for text in ('  "operation": "failed",', '  at Handler.run (synthetic.js:1:2)',
                 'Traceback (most recent call last):', 'File "synthetic.py", line 4',
                 'During handling of the above exception, another exception occurred:'):
        assert not eligible(text, INFO)
        assert raw_context(record(text), INFO)
    for text in ('}', '  ]},  ', 'synthetic operation completed'):
        assert not raw_context(record(text), INFO)
    assert not eligible('}', INFO)  # structural fragments stay raw, never weighted heads
    assert raw_context(record('\x1b[31m  "operation": "failed",\x1b[0m'), INFO)
