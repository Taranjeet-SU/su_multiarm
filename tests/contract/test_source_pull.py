"""``elasticsearch_rest`` source against the fake cluster (addendum A1 slice L1, contract tier):
discovery, per-index counts against generator truth, scroll and ``search_after`` pulls, cursor
expiry restarts the unit, close hygiene (``finally``, exit hook, ``SIGTERM``), the open-context limit,
context fetches, multiline over a pulled day, config/schema wiring and the ``sources`` CLI.
Synthetic data only; the HTTP server, when used, is the in-process fake on 127.0.0.1."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from su_multi_arm.core.config import Config, validate_config
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.sources import LogMapping, SourceCursorExpired, SourceRequestRefused
from su_multi_arm.sources.cursor import _close_everything
from su_multi_arm.sources.multiline import assemble
from su_multi_arm.sources.probe import day_window
from su_multi_arm.testing.fixtures.logs import SyntheticLogSpec, fake_cluster_for
from tests.sources_support import DAY, URL_ENV, wired

ROOT = Path(__file__).resolve().parents[2]
WINDOW = day_window("2026-01-05", "UTC")


def test_discovery_covers_every_index_with_parsed_attributes():
    source, cluster, day = wired()
    units = source.discover(WINDOW)
    assert sorted(u.index for u in units) == sorted(day.indices)
    assert all("unknown" not in u.attributes.values() for u in units)
    assert len({u.id for u in units}) == len(units)
    again, _, _ = wired()
    assert [u.id for u in again.discover(WINDOW)] == [u.id for u in units], "unit ids are stable across runs"
    assert {r.path for r in cluster.requests if r.path.endswith("_mapping")} == {f"/{i}/_mapping" for i in day.indices}


def test_counts_match_generator_truth_with_their_basis():
    source, _, day = wired()
    for unit in source.discover(WINDOW):
        counted = source.count(unit)
        truth = day.truth[unit.index]
        assert counted.total == truth.docs
        assert counted.by_query["errors"] == truth.by_level.get("ERROR", 0)
        node = unit.attributes["service"] in {"query-gateway", "scheduler"}
        assert counted.basis["errors"] == ("level_field" if node else "token_match")


def test_scroll_pull_returns_every_document_once_and_closes_its_context():
    source, cluster, day = wired(pull={"page_size": 17})
    for unit in source.discover(WINDOW)[:3]:
        batches = list(source.pull(unit))
        ids = [d.source_id for b in batches for d in b.docs]
        assert sorted(ids) == sorted(d["_id"] for d in day.indices[unit.index])
        assert batches[-1].done and not batches[-1].capped
    assert not cluster.scrolls and not source.transport.own_ids and not source.cursors.open_cursors()
    deletes = [r for r in cluster.requests if r.method == "DELETE"]
    assert deletes and all(r.path == "/_search/scroll" for r in deletes)


def test_search_after_pull_and_resume():
    source, cluster, day = wired(pull={"cursor": "search_after", "tiebreaker": "log.offset", "page_size": 25})
    unit = source.discover(WINDOW)[0]
    batches = list(source.pull(unit))
    ids = [d.source_id for b in batches for d in b.docs]
    assert len(ids) == len(set(ids)) == len(day.indices[unit.index])
    assert not any("scroll" in r.path or "scroll" in r.params for r in cluster.requests)
    resumed = list(source.pull(unit, cursor=batches[1].cursor))
    assert [d.source_id for b in resumed for d in b.docs] == ids[50:]


def test_cursor_expiry_restarts_the_unit_without_duplicates():
    source, cluster, day = wired(pull={"page_size": 10}, cluster_options={"expire_scroll_after": 3})
    unit = source.discover(WINDOW)[0]
    batches = list(source.pull(unit))
    ids = [d.source_id for b in batches for d in b.docs]
    assert len(ids) == len(set(ids)) == len(day.indices[unit.index])
    assert batches[-1].restarts == 1
    assert not cluster.scrolls


def test_cursor_expiry_beyond_max_restarts_is_a_typed_error():
    source, cluster, _ = wired(pull={"page_size": 10, "max_restarts": 0}, cluster_options={"expire_scroll_after": 2})
    unit = source.discover(WINDOW)[0]
    with pytest.raises(SourceCursorExpired):
        list(source.pull(unit))
    assert not source.cursors.open_cursors()


def test_abandoned_pull_closes_its_scroll_in_finally():
    source, cluster, _ = wired(pull={"page_size": 5})
    unit = source.discover(WINDOW)[0]
    pull = source.pull(unit)
    next(pull)
    assert len(cluster.scrolls) == 1
    pull.close()
    assert not cluster.scrolls and not source.cursors.open_cursors()


def test_exit_hook_closes_open_cursors():
    source, cluster, _ = wired(pull={"page_size": 5})
    unit = source.discover(WINDOW)[0]
    pull = source.pull(unit)
    next(pull)
    _close_everything()
    assert not cluster.scrolls and cluster.closed_scrolls
    pull.close()  # idempotent: nothing more to close
    assert [r.method for r in cluster.requests].count("DELETE") == 1


def test_open_context_limit_is_enforced_before_io():
    source, cluster, _ = wired(pull={"page_size": 5, "max_concurrency": 1, "slices": 1})
    first, second = source.discover(WINDOW)[:2]
    a = source.pull(first)
    next(a)
    sent = len(cluster.requests)
    b = source.pull(second)
    with pytest.raises(SourceRequestRefused, match="open cursor limit"):
        next(b)
    assert len(cluster.requests) == sent + 1  # only the records-tier count of the second unit
    assert cluster.requests[-1].path.endswith("/_count")
    a.close()


def test_context_fetch_is_capped_per_partition():
    source, cluster, day = wired(limits={"max_context_per_partition": 1, "requests_per_second": 50})
    unit = source.discover(WINDOW)[0]
    docs = [d for b in source.pull(unit) for d in b.docs]
    middle = docs[len(docs) // 2]
    around = source.context(middle, before=2, after=2)
    assert len(around) == 4 and middle.source_id not in {d.source_id for d in around}
    with pytest.raises(SourceRequestRefused, match="cap"):
        source.context(middle, 1, 1)
    source.reset_context_budget()
    assert source.context(middle, 1, 0)


def test_pulled_day_maps_and_reassembles_to_generator_truth():
    source, _, day = wired(lines=200)
    for unit in source.discover(WINDOW):
        docs = [d for b in source.pull(unit) for d in b.docs]
        mapping = LogMapping(source.config)
        records = mapping.map(docs, unit, WINDOW.partition_key)
        assert mapping.stats.malformed == 0
        out, stats = assemble(records, source.config.multiline)
        truth = day.truth[unit.index]
        assert stats.fragments == truth.fragments == 1
        assert stats.joined == truth.continuation_docs
        assert len(out) == truth.docs - truth.continuation_docs - truth.fragments
        assert all("\x1b" not in r.message for r in out)


# --- config, schema and CLI ----------------------------------------------------------------------

BASE = {
    "schema_version": 2,
    "dataset": "demo",
    "backends": {"local": {"kind": "memory"}},
    "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
    "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}}},
    "pipeline": {"arms": {"lexical": {"backend": "local"}}},
}
SOURCE = {"kind": "elasticsearch_rest", "url_env": URL_ENV, "indices": ["*-{date}"], "index_name": {"vocabularies": {"service": ["gateway", "query-gateway", "doc-fetcher", "ingest-worker", "scheduler"]}}, "mapping": {"level": "level"}, "counts": {"errors": {"levels": ["ERROR"], "tokens": ["error"]}}}


def test_config_accepts_sources_and_the_schema_lists_them():
    config = validate_config({**BASE, "sources": {"logs": SOURCE}})
    assert config.sources["logs"].pull.keepalive_max == "2m"
    assert "sources" in Config.model_json_schema()["properties"]
    assert validate_config(BASE).sources == {}


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda s: s.update(url="http://x"), "unknown key"),
        (lambda s: s.update(url_env="http://reader:pw@host"), "url_env"),
        (lambda s: s.update(password_env="hunter2!"), "password_env"),
        (lambda s: s.update(auth="basic", username_env="U"), "username_env and password_env"),
        (lambda s: s.update(indices=["_all"]), "index expression"),
        (lambda s: s.update(indices=["logs-{tenant}"]), "placeholder"),
        (lambda s: s.update(timezone="Mars/Olympus"), "timezone"),
        (lambda s: s.update(pull={"records_query": "missing"}), "records_query"),
        (lambda s: s.update(mapping={"level": "level", "drop_fields": ["level"]}), "drop_fields"),
        (lambda s: s.update(multiline={"continuation": ["custom_regex"]}), "continuation"),
        (lambda s: s.update(kind="kafka"), "kind"),
    ],
)
def test_invalid_source_configs_are_refused_with_a_path(mutate, fragment):
    source = json.loads(json.dumps(SOURCE))
    mutate(source)
    with pytest.raises(ConfigError) as caught:
        validate_config({**BASE, "sources": {"logs": source}})
    assert "sources.logs" in str(caught.value) and fragment in str(caught.value)


def test_schema_file_is_regenerated():
    from su_multi_arm.core import schema

    assert not schema.drift(ROOT / "docs/config.schema.json"), "run su_multi_arm.core.schema.write_all('docs')"


@pytest.fixture
def served():
    cluster, day = fake_cluster_for(SyntheticLogSpec(day=DAY, lines_per_index=60))
    server, url = cluster.serve()
    yield cluster, day, url
    server.shutdown()


def write_config(tmp_path: Path, **source_overrides) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({**BASE, "sources": {"logs": {**SOURCE, "limits": {"requests_per_second": 50}, **source_overrides}}}))
    return path


def run_cli(monkeypatch, capsys, *argv):
    from su_multi_arm import cli
    from su_multi_arm.sources import cursor

    monkeypatch.setattr(cursor, "install_signal_handlers", lambda: None)  # keep pytest's handlers
    monkeypatch.setattr(sys, "argv", ["su-multi-arm", *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as exit_:
        code = exit_.code or 0
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_probe_reports_aggregates_only(served, tmp_path, monkeypatch, capsys):
    cluster, day, url = served
    monkeypatch.setenv(URL_ENV, url)
    config = write_config(tmp_path)
    report_path = tmp_path / "probe.json"
    code, out, err = run_cli(monkeypatch, capsys, "--config", str(config), "sources", "probe", "--date", "2026-01-05", "--max-units", "2", "--max-docs", "200", "--output", str(report_path))
    assert code == 0, err
    report = json.loads(report_path.read_text())
    assert report["units_discovered"] == len(day.indices)
    assert report["capabilities"]["probed"] and report["capabilities"]["pit"] and report["capabilities"]["random_score_seq_no"]
    assert report["throughput"]["docs"] == len(next(iter(sorted(day.indices.items())))[1])
    assert report["open_cursors_after"] == 0 and not cluster.scrolls and not cluster.pits
    text = report_path.read_text()
    messages = [d["_source"]["message"] for docs in day.indices.values() for d in docs]
    assert not any(m in text for m in messages if len(m) > 12), "the probe report must not contain log lines"
    assert url not in text


@pytest.mark.parametrize("command", ["check", "discover", "count"])
def test_cli_check_discover_count(served, tmp_path, monkeypatch, capsys, command):
    cluster, day, url = served
    monkeypatch.setenv(URL_ENV, url)
    argv = ["--config", str(write_config(tmp_path)), "sources", command] + ([] if command == "check" else ["--date", "2026-01-05"])
    code, out, err = run_cli(monkeypatch, capsys, *argv)
    assert code == 0, err
    payload = json.loads(out)
    if command == "check":
        assert payload["self_check"][0]["code"] == "source_read_only"
    elif command == "discover":
        assert len(payload["units"]) == len(day.indices)
    else:
        assert sum(c["total"] for c in payload["counts"]) == sum(len(v) for v in day.indices.values())
    assert not any(r.method not in {"GET", "POST"} for r in cluster.requests)


def test_cli_check_fails_on_a_write_privilege_under_require_read_only_role(served, tmp_path, monkeypatch, capsys):
    cluster, _, url = served
    cluster.privileges = {"index": True}
    monkeypatch.setenv(URL_ENV, url)
    code, out, _ = run_cli(monkeypatch, capsys, "--config", str(write_config(tmp_path, require_read_only_role=True)), "sources", "check")
    assert code == 1 and json.loads(out)["self_check"][0]["severity"] == "error"


def test_cli_without_the_url_variable_is_a_typed_error(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(URL_ENV, raising=False)
    code, _, err = run_cli(monkeypatch, capsys, "--config", str(write_config(tmp_path)), "sources", "check")
    assert code == 1 and "SourceUnavailable" in err and URL_ENV in err


def test_sigterm_closes_open_scrolls(served):
    cluster, _, url = served
    script = textwrap.dedent(
        f"""
        import json, os, signal
        from su_multi_arm.core.config_sources import SourceConfig
        from su_multi_arm.sources import build_source
        from su_multi_arm.sources.cursor import install_signal_handlers
        from su_multi_arm.sources.probe import day_window
        cfg = SourceConfig.model_validate(json.loads({json.dumps({**SOURCE, "pull": {"page_size": 5}})!r}))
        install_signal_handlers()
        src = build_source("logs", cfg)
        unit = src.discover(day_window("2026-01-05", "UTC"))[0]
        pull = src.pull(unit)
        next(pull)
        os.kill(os.getpid(), signal.SIGTERM)
        """
    )
    env = {**os.environ, URL_ENV: url, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 128 + signal.SIGTERM, result.stderr
    assert cluster.closed_scrolls and not cluster.scrolls
