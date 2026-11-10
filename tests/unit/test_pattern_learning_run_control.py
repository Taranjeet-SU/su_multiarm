"""Run control of ``patterns learn|propose`` (real-run fixes, 2026-11-04): a clean stop between
calls through the stop file, the report persisted after every cluster, and no new call for a
template whose proposal waits for review. Scripted fake provider (``httpx.MockTransport``)."""

from __future__ import annotations

import json

from su_multi_arm.learning.patterns import session_from_config
from tests.pattern_fixtures import engine_or_skip
from tests.pattern_learning_support import NOW, FakeLearner, by_template, clock, learning_lines, library_dir, make_config


def session(tmp_path, fake, **config):
    engine_or_skip(config.get("engine", "regex"))
    library = tmp_path / "patterns"
    if not library.exists():
        library_dir(library)
    cfg = make_config(tmp_path, library, **config)
    return session_from_config(cfg, transport=fake.transport(), clock=clock), library


def test_the_stop_file_stops_between_calls_and_the_run_is_paused_not_failed(tmp_path):
    fake = FakeLearner()
    learn, library = session(tmp_path, fake, promotion="manual")
    calls = []

    def script(payload):
        calls.append(payload["cluster_id"])
        (library / ".learn.stop").write_text("stop")  # requested during the first call
        return by_template(payload)

    fake.script = script
    report = learn.run(learning_lines(), mode="propose", now=NOW).to_dict()
    assert len(calls) == 1 and report["paused"] is True and report["stopped"] is None
    assert any(c["outcome"] == "not_sent" and c.get("reason") == "stop requested" for c in report["clusters"])


def test_the_report_is_persisted_after_every_cluster(tmp_path):
    fake = FakeLearner()
    learn, _ = session(tmp_path, fake, promotion="manual")
    path = tmp_path / "partial.json"
    seen = []

    def progress(partial):
        seen.append(len(partial["clusters"]))
        path.write_text(json.dumps(partial))

    learn.run(learning_lines(), mode="propose", now=NOW, progress=progress)
    assert seen and seen == sorted(seen) and json.loads(path.read_text())["partial"] is True


def test_a_template_with_a_pending_proposal_gets_no_new_call(tmp_path):
    fake = FakeLearner()
    learn, library = session(tmp_path, fake, promotion="manual")
    first = learn.run(learning_lines(), mode="propose", now=NOW).to_dict()
    assert first["written"]["proposed"]
    fake.requests.clear()
    again, _ = session(tmp_path, fake, promotion="manual")
    second = again.run(learning_lines(), mode="propose", now=NOW).to_dict()
    pending = [c for c in second["clusters"] if c["outcome"] == "pending_proposal"]
    assert pending and {c["entry_id"] for c in pending} <= set(first["written"]["proposed"])
    sent = {json.loads(r["messages"][-1]["content"])["cluster_id"] for r in fake.requests}
    assert not sent & {c["cluster_id"] for c in pending}
