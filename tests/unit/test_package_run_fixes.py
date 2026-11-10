"""Package fixes from the first real deployment run of the log features, reproduced with synthetic
names and lines only: probe PIT opt-in and deployment ``disallow`` (P1), one privilege request
(P14), the content-free request log (P18), the index-name prefix and deployment position (P2, P3),
continuation rules for closing brackets and column-0 Python tracebacks plus library
``continuation`` entries (P4), multiline-aware ``patterns coverage|mine`` (P5), noise precedence
(P6) and the full lint report (P7)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from su_multi_arm.core.config_sources import IndexNameConfig, MultilineConfig
from su_multi_arm.extraction.patterns.extractor import CompiledLibrary
from su_multi_arm.extraction.patterns.library import load_library
from su_multi_arm.sources.errors import SourceRequestRefused
from su_multi_arm.sources.logs import IndexNameParser, LogRecord
from su_multi_arm.sources.multiline import assemble, continuation_predicate
from su_multi_arm.sources.probe import day_window, run_probe
from su_multi_arm.testing.fixtures.logs import SERVICES
from tests.pattern_fixtures import engine_or_skip, write_library
from tests.sources_support import DAY, wired
from tests.unit.test_config_cli_a2 import run_main

ENGINES = ["re2", "regex"]


# --- P1 / P14 / P18: probe -----------------------------------------------------------------------


def pit_rows(cluster) -> list[str]:
    return [r.path for r in cluster.requests if "_pit" in r.path or "point_in_time" in r.path]


def test_probe_sends_no_pit_request_by_default():
    source, cluster, _ = wired(lines=20)
    report = run_probe(source, DAY.isoformat(), max_units=2, max_docs=0)
    assert pit_rows(cluster) == []
    assert report["pit_check"] == {"status": "skipped", "reason": "not_requested"}
    assert all("pit" not in unit for unit in report["units"])


def test_probe_skips_pit_when_the_version_is_unknown_even_if_requested():
    source, cluster, _ = wired(lines=20, cluster_options={"forbid_root": True})  # 403 on GET /: version unknown
    report = run_probe(source, DAY.isoformat(), max_units=2, max_docs=0, probe_pit=True)
    assert pit_rows(cluster) == []
    assert report["pit_check"]["reason"] == "unsupported_or_unknown_version" and report["capabilities"]["pit"] is False


def test_probe_pit_runs_only_on_request_and_when_supported():
    source, cluster, _ = wired(lines=20)
    report = run_probe(source, DAY.isoformat(), max_units=1, max_docs=0, probe_pit=True)
    assert report["pit_check"]["status"] == "run" and report["units"][0]["pit"] is True
    assert pit_rows(cluster) and not cluster.pits  # opened and closed


def test_a_disallowed_request_family_is_refused_before_io_and_skipped_by_the_probe():
    source, cluster, _ = wired(lines=20, disallow=["pit"])
    report = run_probe(source, DAY.isoformat(), max_units=1, max_docs=0, probe_pit=True)
    assert report["pit_check"]["reason"] == "disallowed_by_config" and pit_rows(cluster) == []
    unit = source.discover(day_window(DAY.isoformat(), "UTC"))[0]
    sent = source.transport.sent
    with pytest.raises(SourceRequestRefused, match="disallowed"):
        source.probe_pit(unit)
    assert source.transport.sent == sent and pit_rows(cluster) == []


def test_privileges_are_requested_once_per_source():
    source, cluster, _ = wired(lines=20)
    run_probe(source, DAY.isoformat(), max_units=1, max_docs=0)
    source.self_check()
    assert sum(1 for r in cluster.requests if "_has_privileges" in r.path) == 1


def test_the_request_log_is_content_free():
    source, cluster, day = wired(lines=20)
    run_probe(source, DAY.isoformat(), max_units=1, max_docs=50)
    rows = source.transport.request_log()
    assert rows and {"method", "row", "path", "status", "ms", "bytes"} == set(rows[0])
    text = json.dumps(rows)
    assert not any(name in text for name in day.indices)  # templates, never index names


# --- P2 / P3: index-name grammar -------------------------------------------------------------------


def test_the_literal_prefix_never_reaches_the_tenant():
    parser = IndexNameParser(IndexNameConfig(prefix="logs", vocabularies={"service": ["kafka"]}))
    parsed = parser.parse("logs-demo-production-kafka-2026.01.01")
    assert parsed.attributes == {"tenant": "demo", "env": "production", "service": "kafka", "index_date": "2026-01-01"}
    with pytest.raises(ValueError):
        IndexNameConfig(prefix="logs-")  # the separator is not part of the prefix


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("logs-demo-production-kafka-2026.01.01", {"deployment": "none", "service": "kafka"}),
        ("logs-demo-production-ops-admin-kafka-2026.01.01", {"deployment": "ops-admin", "service": "kafka"}),
        ("logs-demo-production-node-12-search-client-2026.01.01", {"deployment": "node-12", "service": "search-client"}),
        ("logs-acme-sandbox-ops-admin-slack_bot-2026.01.01", {"deployment": "ops-admin", "service": "slack_bot", "tenant": "acme", "env": "sandbox"}),
    ],
)
def test_a_deployment_prefix_between_env_and_service(name, expected):
    parser = IndexNameParser(
        IndexNameConfig(prefix="logs", deployment_prefixes=["ops-admin", "node-{n}"], deployment_position="before_service", vocabularies={"service": ["kafka", "search", "search-client", "slack_bot"]})
    )
    parsed = parser.parse(name)
    assert parsed.ok, parsed.problem
    assert {**{"tenant": "demo", "env": "production"}, **expected}.items() <= parsed.attributes.items()


@pytest.mark.parametrize(
    ("name", "reason", "services"),
    [
        ("logs-demo-production-other-kafka-2026.01.01", "segments", ["kafka"]),  # undeclared deployment: never a guess
        ("logs-demo-production-ops-admin-kafka-2026.01.01", "ambiguous", ["kafka", "ops-admin-kafka"]),  # fits two ways
        ("demo-production-kafka-2026.01.01", "prefix", ["kafka"]),
    ],
)
def test_before_service_names_that_do_not_fit_once_are_unparsed(name, reason, services):
    parser = IndexNameParser(IndexNameConfig(prefix="logs", deployment_prefixes=["ops-admin"], deployment_position="before_service", vocabularies={"service": services}))
    parsed = parser.parse(name)
    assert not parsed.ok and parsed.reason == reason
    assert parsed.attributes["deployment"] == "unknown" and parsed.attributes["tenant"] == "unknown"


def test_prefix_and_deployment_end_to_end_through_discovery():
    """The fake cluster's synthetic names carry no prefix; a source reading prefixed names through
    the same grammar yields stripped tenants and the deployment attribute on every unit."""
    from su_multi_arm.core.config_sources import SourceConfig
    from su_multi_arm.sources import build_source
    from su_multi_arm.testing.fixtures.logs import FakeLogCluster
    from tests.sources_support import ENV, URL_ENV, no_wait

    stamp = DAY.strftime("%Y.%m.%d")
    mapping = {"mappings": {"properties": {"@timestamp": {"type": "date"}, "message": {"type": "text"}}}}
    names = [f"logs-demo-production-kafka-{stamp}", f"logs-demo-production-ops-admin-kafka-{stamp}"]
    cluster = FakeLogCluster(indices={n: [] for n in names}, mappings={n: mapping for n in names})
    cfg = SourceConfig.model_validate(
        {
            "kind": "elasticsearch_rest",
            "url_env": URL_ENV,
            "indices": ["logs-*-{date}"],
            "index_name": {"prefix": "logs", "deployment_prefixes": ["ops-admin"], "deployment_position": "before_service", "vocabularies": {"service": ["kafka"]}},
        }
    )
    source = build_source("synthetic", cfg, env=ENV, http_transport=cluster.transport())
    source._rate_limiter = no_wait()

    units = sorted(source.discover(day_window(DAY.isoformat(), "UTC")), key=lambda u: u.index)
    assert [(u.attributes["tenant"], u.attributes["deployment"], u.attributes["service"]) for u in units] == [("demo", "none", "kafka"), ("demo", "ops-admin", "kafka")]
    assert source.diagnostics == []


ENV_ANCHORED = {"prefix": "logs", "deployment_position": "after_env", "vocabularies": {"env": ["production", "sandbox"], "service": ["kafka", "search-client"]}}


@pytest.mark.parametrize(
    ("name", "deployment", "service"),
    [
        ("logs-demo-production-kafka-2026.01.01", "none", "kafka"),
        ("logs-demo-production-ops-admin-kafka-2026.01.01", "ops-admin", "kafka"),
        ("logs-acme-sandbox-edge-proxy-batch-tier-search-client-2026.01.01", "edge-proxy-batch-tier", "search-client"),
    ],
)
def test_env_anchored_deployment_needs_no_declared_list(name, deployment, service):
    parsed = IndexNameParser(IndexNameConfig.model_validate(ENV_ANCHORED)).parse(name)
    assert parsed.ok, parsed.problem
    assert (parsed.attributes["deployment"], parsed.attributes["service"]) == (deployment, service)


def test_env_anchored_needs_its_anchors():
    with pytest.raises(ValueError, match="after_env"):
        IndexNameConfig.model_validate({"deployment_position": "after_env", "vocabularies": {"service": ["kafka"]}})
    parser = IndexNameParser(IndexNameConfig.model_validate(ENV_ANCHORED))
    assert parser.parse("logs-demo-staging-kafka-2026.01.01").reason == "vocabulary"  # unknown env: never a guess


def test_unparsed_names_can_be_excluded_and_are_counted():
    source, _, day = wired(lines=10, index_name={"vocabularies": {"service": ["query-gateway"]}, "unparsed": "exclude"})
    units = source.discover(day_window(DAY.isoformat(), "UTC"))
    assert units and all(u.attributes["service"] == "query-gateway" for u in units)
    assert source.excluded_scope["unparsed_name"] == len(day.indices) - len(units)


# --- P4: continuation rules ------------------------------------------------------------------------


def rec(i, message, ms=0, container="c1"):
    t0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
    return LogRecord(id=f"r{i:03d}", ts=t0 + timedelta(milliseconds=ms), partition_key="p", message=message, container=container, file="f", offset=i, source_id=f"s{i}", index="i")


def test_closing_brackets_and_column_zero_tracebacks_join_their_head():
    lines = [
        "[ERROR] request failed with payload {",
        '  "code": 7,',
        "}",
        "});",
        "[ERROR] worker crashed",
        "Traceback (most recent call last):",
        'File "/srv/app/worker.py", line 12, in run',
        "result = compute(item)",
        "    ^^^^^^^^^^^^^",
        "ValueError: synthetic failure",
        "",
        "During handling of the above exception, another exception occurred:",
        "Traceback (most recent call last):",
        'File "/srv/app/main.py", line 3, in <module>',
        "main()",
        "RuntimeError: wrapped",
        "[INFO] next request",
    ]
    records = [rec(i, line, i) for i, line in enumerate(lines) if line]
    config = MultilineConfig(continuation=["indented", "java_frame", "python_frame", "traceback_header", "python_traceback", "caused_by", "ellipsis_more", "closing_bracket"])
    out, stats = assemble(records, config)
    assert [r.message for r in out] == ["[ERROR] request failed with payload {", "[ERROR] worker crashed", "[INFO] next request"]
    assert out[1].lines == 11 and out[1].stack.splitlines()[-1] == "RuntimeError: wrapped"
    assert stats.fragments == 0


def test_default_rules_cover_brackets_and_tracebacks():
    cont = continuation_predicate(MultilineConfig().continuation)
    for text in ("}", "]", ")", "});", "},", 'File "x.py", line 1, in f', "The above exception was the direct cause of the following exception:"):
        assert cont(text), text
    for text in ("ValueError: a standalone log line", "[ERROR] x", "{"):
        assert not cont(text), text


def test_an_exception_line_outside_a_traceback_stays_a_head():
    out, stats = assemble([rec(0, "[INFO] started"), rec(1, "ValueError: config value rejected", 1)], MultilineConfig())
    assert [r.message for r in out] == ["[INFO] started", "ValueError: config value rejected"] and stats.fragments == 0


def test_headless_bracket_and_traceback_lines_are_fragments():
    out, stats = assemble([rec(0, "}", 0), rec(1, "Traceback (most recent call last):", 10_000), rec(2, "[INFO] ok", 20_000)], MultilineConfig())
    assert [r.message for r in out] == ["[INFO] ok"] and stats.fragments == 2


def continuation_library(tmp_path, kind: str):
    entry = {
        "id": "cont.json_field",
        "kind": "continuation",
        "status": "approved",
        "regex": r'^[ \t]{0,8}"[a-z_]{1,32}": [^\n]{1,200}$',
        "examples": {"positive": ['  "code": 7,'], "negative": ["[ERROR] x"]},
        "provenance": {"author": "user"},
    }
    root = write_library(tmp_path / "lib", [entry])
    engine = engine_or_skip(kind)
    return CompiledLibrary(load_library(root, engine), engine, 8192)


@pytest.mark.parametrize("kind", ENGINES)
def test_approved_library_continuation_entries_join_lines(tmp_path, kind):
    library = continuation_library(tmp_path, kind)
    records = [rec(0, "[ERROR] payload dump"), rec(1, '"code": 7,', 1), rec(2, '"detail": "boom"', 2), rec(3, "[INFO] next", 3)]
    plain, _ = assemble(records, MultilineConfig())
    joined, _ = assemble(records, MultilineConfig(), library=library)
    assert len(plain) == 4 and [r.message for r in joined] == ["[ERROR] payload dump", "[INFO] next"]


def test_continuation_entries_take_no_selectors(tmp_path):
    engine = engine_or_skip("regex")
    entry = {"id": "cont.x", "kind": "continuation", "status": "approved", "applies_to": {"service": "kafka"}, "regex": r"^[ \t]{1,8}at [a-z.]{1,64}$", "provenance": {"author": "user"}}
    library = load_library(write_library(tmp_path / "lib", [entry]), engine, strict=False)
    assert "continuation_selector" in {i.code for i in library.errors()}


# --- P6: noise precedence --------------------------------------------------------------------------


def noise_library(tmp_path, kind: str, precedence: str | None):
    message = {
        "id": "msg.http.access",
        "kind": "message",
        "status": "approved",
        "priority": 10,
        "regex": r"^\[(?P<method>[A-Z]{3,7})\] \[[^\]]{0,64}\] \[(?P<path>[^\]]{0,64})\] \[(?P<status>[0-9]{3})\]$",
        "signature": {"id": "http.access", "category": "http"},
        "provenance": {"author": "user"},
    }
    noise = {"id": "noise.health", "kind": "noise", "status": "approved", "regex": r"/health\] \[[0-9]{3}\]$", "provenance": {"author": "user"}}
    if precedence:
        noise["precedence"] = precedence
    engine = engine_or_skip(kind)
    return CompiledLibrary(load_library(write_library(tmp_path / f"lib-{precedence}", [message, noise]), engine), engine, 8192)


@pytest.mark.parametrize("kind", ENGINES)
def test_a_before_message_noise_rule_wins_over_a_message(tmp_path, kind):
    line = "[GET] [<ip>] [/x/health] [200]"
    other = "[GET] [<ip>] [/x/items] [200]"
    default = noise_library(tmp_path, kind, None)
    assert default.match_line(line).status == "parsed"  # unchanged default: noise only after messages
    first = noise_library(tmp_path, kind, "before_message")
    result = first.match_line(line)
    assert result.status == "noise" and result.signature is None and result.annotations == []
    assert first.match_line(other).status == "parsed"
    assert default.identity != first.identity


def test_precedence_is_noise_only_and_unset_keeps_identities(tmp_path):
    engine = engine_or_skip("regex")
    bad = {"id": "msg.x", "kind": "message", "status": "approved", "precedence": "before_message", "regex": r"^x [0-9]{1,3}$", "signature": {"id": "x"}, "provenance": {"author": "user"}}
    library = load_library(write_library(tmp_path / "bad", [bad]), engine, strict=False)
    assert "precedence_kind" in {i.code for i in library.errors()}
    good = dict(bad, precedence=None)
    loaded = load_library(write_library(tmp_path / "good", [good]), engine)
    assert "precedence" not in loaded.entries["msg.x"].canonical()


# --- P5 / P7: patterns CLI -------------------------------------------------------------------------


def cli_config(tmp_path, library_dir, engine: str):
    data = {
        "schema_version": 2,
        "dataset": "logs",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "state")}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 16}}},
        "pipeline": {"arms": {"lexical": {"backend": "local"}}},
        "extraction": {"extractors": ["pattern"], "pattern_library": {"path": str(library_dir), "engine": engine}},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.mark.parametrize("kind", ENGINES)
def test_coverage_cli_counts_fragments_like_ingest(tmp_path, monkeypatch, capsys, kind):
    engine_or_skip(kind)
    message = {"id": "msg.crash", "kind": "message", "status": "approved", "regex": r"^\[ERROR\] worker crashed$", "signature": {"id": "worker.crash"}, "provenance": {"author": "user"}}
    root = write_library(tmp_path / "lib", [message])
    config = cli_config(tmp_path, root, kind)
    lines = tmp_path / "lines.txt"
    lines.write_text("}\n[ERROR] worker crashed\nTraceback (most recent call last):\nFile \"/srv/w.py\", line 3, in run\nrun()\nKeyError: 'k'\n}\n[INFO] unknown line\n")
    code, out, err = run_main(monkeypatch, capsys, "--config", str(config), "patterns", "coverage", "--input", str(lines))
    assert code == 0, err
    report = json.loads(out)
    # 8 lines: 1 head-less "}" fragment, 5 joined continuation lines, 2 heads (1 parsed, 1 unparsed)
    assert report["fragments"] == 1 and report["lines"] == 3 and report["by_status"] == {"fragment": 1, "parsed": 1, "unparsed": 1}
    assert report["coverage"] == 0.5 and report["multiline"]["joined"] == 5
    code, out, _ = run_main(monkeypatch, capsys, "--config", str(config), "patterns", "coverage", "--input", str(lines), "--no-multiline")
    assert json.loads(out)["lines"] == 8 and json.loads(out)["fragments"] == 0


def test_coverage_cli_reads_jsonl_records_with_streams_and_times(tmp_path, monkeypatch, capsys):
    engine_or_skip("regex")
    message = {"id": "msg.crash", "kind": "message", "status": "approved", "regex": r"^\[ERROR\] worker crashed$", "signature": {"id": "worker.crash"}, "provenance": {"author": "user"}}
    config = cli_config(tmp_path, write_library(tmp_path / "lib", [message]), "regex")
    rows = [
        {"id": "a", "text": "[ERROR] worker crashed\n  at frame", "ts": "2026-01-05T00:00:00Z", "attributes": {"container": "c1", "level": "error"}},
        {"id": "b", "text": "}", "ts": "2026-01-05T00:00:00.001Z", "attributes": {"container": "c2"}},  # other stream: fragment
        {"id": "c", "text": "}", "ts": "2026-01-05T00:00:00.002Z", "attributes": {"container": "c1"}},
        {"id": "d", "text": "}", "ts": "2026-01-05T00:01:00Z", "attributes": {"container": "c1"}},  # beyond max_gap_ms
    ]
    path = tmp_path / "rows.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    code, out, err = run_main(monkeypatch, capsys, "--config", str(config), "patterns", "coverage", "--input", str(path))
    assert code == 0, err
    report = json.loads(out)
    assert report["by_status"] == {"fragment": 2, "parsed": 1} and report["coverage"] == 1.0


def test_lint_reports_every_issue_and_exits_non_zero(tmp_path, monkeypatch, capsys):
    engine_or_skip("regex")
    bad = [{"id": f"msg.bad{i}", "kind": "message", "status": "approved", "regex": r"^bad .* end", "signature": {"id": f"bad{i}"}, "provenance": {"author": "user"}} for i in range(25)]
    config = cli_config(tmp_path, write_library(tmp_path / "lib", bad), "regex")
    code, out, err = run_main(monkeypatch, capsys, "--config", str(config), "patterns", "lint")
    assert code == 1
    report = json.loads(out)
    assert report["errors"] == 25 and len([r for r in report["issue_rows"] if r["severity"] == "error"]) == 25
    assert report["stopped"] == "25 lint error(s)"


def test_services_fixture_is_unchanged():
    assert "query-gateway" in SERVICES
