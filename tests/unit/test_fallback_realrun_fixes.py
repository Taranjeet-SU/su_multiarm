"""Egress and run-control fixes from a real learning run (2026-11-04), reproduced on synthetic data
with a scripted fake provider: case-insensitive, token-boundary pseudonymisation; the B11
redactor set (cookie, session and tracking values) in the egress shape check; a model-labelled noise
pattern goes to a human as noise; a clean stop between calls; an incrementally persisted report."""

from __future__ import annotations

import json

import pytest

from su_multi_arm.learning.patterns.egress import Pseudonymiser, egress_shapes
from su_multi_arm.learning.patterns.mining import Cluster
from su_multi_arm.learning.patterns.propose import PatternProposalReply, proposal_entry
from tests.fallback_support import DAY, FakeProvider, cluster_reply, config, heads_of, load, seal_day, session, write_library
from tests.logs_support import ENGINE
from tests.pattern_fixtures import engine_or_skip

KEY = DAY.isoformat()


@pytest.fixture(autouse=True)
def _engine():
    engine_or_skip(ENGINE)


def test_pseudonymisation_is_case_insensitive_and_token_bounded():
    pseudo = Pseudonymiser(["tenant", "host"])
    pseudo.attributes({"tenant": "acme", "host": "node-a1"})
    text = pseudo.text("ACME login from Acme at node-a1 and NODE-A1; acmeish stays; xacme stays")
    assert "acme" not in text.casefold().replace("acmeish", "").replace("xacme", "")
    assert text.startswith("tenant-1 login from tenant-1 at host-1 and host-1")
    assert "acmeish" in text and "xacme" in text  # not inside a longer identifier
    assert pseudo.alias("tenant", "ACME") == "tenant-1"  # one alias per value, whatever the case


@pytest.mark.parametrize(
    "text",
    [
        "Cookie: sessionid=abc123def456ghi; theme=dark",
        "socket.io handshake sid=Zx81kq0PqAAAB2 transport=polling",
        "page view _ga=GA1.2.1234567.89012345 path /home",
    ],
)
def test_egress_detects_cookie_session_and_tracking_values(text):
    assert "tracking_id" in egress_shapes(text)


def test_egress_does_not_flag_template_text():
    for text in ("upstream timeout calling redis after 100 ms", "login refused for [REDACTED_EMAIL]", "cache miss for key 0000000a-0000-4000-8000-00000000000a in 3 ms"):
        assert egress_shapes(text) == []


def test_a_tracking_value_line_is_refused_on_its_own(tmp_path):
    write_library(tmp_path / "patterns")
    cfg = config(tmp_path, extraction={"egress": {"max_refused_share": 0.5}})
    rows = [("WARN", "disk quota of volume data reached 91 percent"), ("WARN", "page view _ga=GA1.2.1234567.89012345 path /home")]
    store = seal_day(cfg, rows=rows)
    fake = FakeProvider()
    report = session(cfg, fake).run(store, [KEY], load(cfg))
    heads = heads_of(store, report)
    assert heads[f"{KEY}-h001"].reason == "egress_refused" and "GA1.2" not in fake.sent_text()
    assert heads[f"{KEY}-h000"].final_status == "model_line"


def test_model_labelled_noise_becomes_a_noise_entry_for_a_human(tmp_path):
    write_library(tmp_path / "patterns")
    library = load(config(tmp_path)).library
    reply = PatternProposalReply(
        cluster_id="c-1", action="new", issue_name="health probe", issue_category="noise", regex=r"^health probe ok in (?P<ms>[0-9]{1,4}) ms$", rationale="benign",
    )
    made = proposal_entry(reply, cluster_id="c-1", cluster=Cluster(1, "svc", "info", ["health"]), supplied={}, library=library, model="m")
    assert made.entry is not None and made.entry.kind == "noise"  # auto-promotion only takes message entries


def test_labels_with_a_noise_category_write_a_noise_proposal(tmp_path):
    def script(payload):
        reply = cluster_reply(payload)
        if payload["task"] == "cluster" and payload["template"].startswith("upstream"):
            reply["issue_category"] = "noise"
        return reply

    write_library(tmp_path / "patterns")
    cfg = config(tmp_path, promotion="auto")
    store = seal_day(cfg)
    report = session(cfg, FakeProvider(cluster=script)).run(store, [KEY], load(cfg))
    noise = [p for p in report["proposals"] if p["kind"] == "noise"]
    assert noise and all(p["write"] != "learned" for p in noise)  # never auto-promoted


def test_a_stop_file_stops_between_calls_and_defers_the_rest(tmp_path):
    write_library(tmp_path / "patterns")
    stop = tmp_path / "stop.flag"
    cfg = config(tmp_path, extraction={"stop_file": str(stop)})
    store = seal_day(cfg)
    fake = FakeProvider()
    fallback = session(cfg, fake)
    calls = []

    def cluster(payload):
        calls.append(payload["task"])
        stop.write_text("stop")  # requested during the first call: no second call
        return cluster_reply(payload)

    fake.cluster = cluster
    report = fallback.run(store, [KEY], load(cfg))
    assert calls == ["cluster"] and fake.tasks() == ["cluster"]
    assert report["state"]["pattern_learner"] == "paused"
    reasons = {h.reason for h in heads_of(store, report).values() if h.final_status == "unresolved"}
    assert "capacity_deferred" in reasons  # retried by the next run
    stop.unlink()
    retry = session(cfg, FakeProvider()).run(store, [KEY], load(cfg))
    assert "capacity_deferred" not in {h.reason for h in heads_of(store, retry).values()}


def test_the_run_report_is_persisted_incrementally(tmp_path):
    write_library(tmp_path / "patterns")
    cfg = config(tmp_path)
    store = seal_day(cfg)
    path = tmp_path / "partial.json"
    stages = []

    def progress(report):
        stages.append(report["stage"])
        path.write_text(json.dumps(report, default=str))

    session(cfg, FakeProvider()).run(store, [KEY], load(cfg), progress=progress)
    assert stages[0] == "egress" and stages.count("clusters") == 2 and stages[-1] == "done"
    assert json.loads(path.read_text())["partitions"][KEY]["status"] == "written"
