"""``su-multi-arm patterns propose|learn|promote|deprecate|disable`` and the ``learning.patterns``
configuration (addendum A1 §3.6, slice L4b). The provider's HTTP call is served by a scripted
``httpx.MockTransport`` (no network, no key, no spend). Synthetic data only."""

from __future__ import annotations

import json

import pytest
import yaml

from su_multi_arm.core.config import load_config
from su_multi_arm.core.errors import ConfigError
from tests.pattern_fixtures import engine_or_skip
from tests.pattern_learning_support import HOSTED_ROLE, FakeLearner, config_data, learning_lines, library_dir
from tests.unit.test_config_cli_a2 import run_main


@pytest.fixture
def fake(monkeypatch):
    engine_or_skip("regex")  # every test of a learning run uses the default development engine
    from su_multi_arm.providers import base

    learner = FakeLearner()
    original = base.HttpCall.__init__

    def patched(self, transport=None):
        original(self, learner.transport())

    monkeypatch.setattr(base.HttpCall, "__init__", patched)
    monkeypatch.setenv("SU_TEST_FAKE_KEY", "sk-offline-not-a-key")
    return learner


def setup(tmp_path, **kwargs):
    library = library_dir(tmp_path / "patterns")
    data = config_data(tmp_path, library, **kwargs)
    data["extraction"]["pattern_library"]["path"] = "patterns"  # relative to the config file
    data["learning"]["patterns"]["spend_ledger"] = "state/spend.sqlite"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    lines = tmp_path / "lines.jsonl"
    lines.write_text("".join(json.dumps({"id": li.id, "text": li.text, "attributes": li.attributes}) + "\n" for li in learning_lines()))
    return path, library, lines


def cli(monkeypatch, capsys, config, *argv):
    code, out, err = run_main(monkeypatch, capsys, "--config", str(config), "patterns", *argv)
    return code, (json.loads(out) if out.strip().startswith("{") else out), err


def test_learn_writes_proposals_and_reports_coverage(tmp_path, monkeypatch, capsys, fake):
    config, library, lines = setup(tmp_path, promotion="manual")
    user_bytes = (library / "library.yaml").read_bytes()
    code, report, _ = cli(monkeypatch, capsys, config, "learn", "--input", str(lines))
    assert code == 0 and report["written"]["proposed"] and report["coverage"]["before"]["coverage"] == 0.16
    assert report["coverage"]["after_if_approved"]["coverage"] > report["coverage"]["before"]["coverage"]
    assert len(fake.requests) == 3 and (tmp_path / "state" / "spend.sqlite").exists()
    assert (library / "library.yaml").read_bytes() == user_bytes
    first = report["written"]["proposed"][0]
    code, out, _ = cli(monkeypatch, capsys, config, "promote", first, "--by", "owner", "--reason", "reviewed")
    assert code == 0 and out["decision"]["op"] == "approve" and out["decision"]["by"] == "user:owner"
    code, report, _ = cli(monkeypatch, capsys, config, "coverage", "--input", str(lines))
    assert code == 0 and report["by_status"]["parsed"] == 16
    assert cli(monkeypatch, capsys, config, "deprecate", first)[0] == 0
    assert cli(monkeypatch, capsys, config, "disable", first)[0] == 0
    code, _, err = cli(monkeypatch, capsys, config, "promote", first)
    assert code == 1 and "DecisionRefused" in err and "disabled" in err
    assert (library / "library.yaml").read_bytes() == user_bytes


def test_propose_dry_run_and_output_file(tmp_path, monkeypatch, capsys, fake):
    config, library, lines = setup(tmp_path)
    code, report, _ = cli(monkeypatch, capsys, config, "propose", "--input", str(lines), "--dry-run")
    assert code == 0 and report["dry_run"] and {c["outcome"] for c in report["clusters"]} == {"would_send"}
    assert fake.requests == [] and sorted(p.name for p in library.iterdir()) == ["library.yaml"]
    out = tmp_path / "report.json"
    code, summary, _ = cli(monkeypatch, capsys, config, "propose", "--input", str(lines), "--output", str(out))
    assert code == 0 and summary == {"written": str(out), "stopped": None}
    report = json.loads(out.read_text())
    assert report["mode"] == "propose" and report["policy"] == "manual" and not (library / "learned.yaml").exists()


def test_a_budget_stop_exits_non_zero(tmp_path, monkeypatch, capsys, fake):
    config, library, lines = setup(tmp_path, learning={"max_calls_per_run": 1, "max_calls_per_day": 1})
    code, report, _ = cli(monkeypatch, capsys, config, "learn", "--input", str(lines))
    assert code == 1 and report["stopped"] == "budget_exhausted:calls" and len(fake.requests) == 1
    code, report, _ = cli(monkeypatch, capsys, config, "learn", "--input", str(lines))
    assert code == 1 and report["stopped"] == "budget_exhausted:day_calls" and len(fake.requests) == 1


def test_egress_refusal_is_a_concise_error(tmp_path, monkeypatch, capsys, fake):
    config, _, lines = setup(tmp_path, privacy={"redact_emails": True})
    lines.write_text(lines.read_text() + json.dumps({"id": "ip", "text": 'ts=2026-11-04T10:00:00.000Z lvl=warn comp=a msg="peer 198.51.100.7 gone"'}) + "\n")
    code, _, err = cli(monkeypatch, capsys, config, "learn", "--input", str(lines))
    assert code == 1 and "EgressRefused" in err and "ip: unredacted ipv4" in err and fake.requests == []


def test_a_hosted_role_uses_strict_structured_output(tmp_path, monkeypatch, capsys, fake):
    config, library, lines = setup(
        tmp_path, role=HOSTED_ROLE, price_table=True, learning={"max_usd_per_run": 3, "max_usd_per_day": 5, "egress": {"allow_hosted": True}}, promotion="manual"
    )
    code, report, _ = cli(monkeypatch, capsys, config, "learn", "--input", str(lines), "--max-usd", "2")
    assert report["spend"]["limits"]["max_usd"] == 2
    sent = fake.requests[0]
    assert sent["response_format"]["type"] == "json_schema" and sent["response_format"]["json_schema"]["strict"] is True
    assert report["spend"]["usd"] == pytest.approx(0.52 * len(fake.requests)) and code in (0, 1)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["models"]["roles"].pop("pattern_learner"), "models.roles.pattern_learner"),
        (lambda d: d["extraction"].pop("pattern_library"), "extraction.pattern_library"),
        (lambda d: d["models"]["roles"].update(pattern_learner=HOSTED_ROLE), "allow_hosted"),
        (lambda d: (d["models"]["roles"].update(pattern_learner=HOSTED_ROLE), d["learning"]["patterns"].update(egress={"allow_hosted": True})), "max_usd_per_run and max_usd_per_day"),
        (lambda d: d["learning"]["patterns"].update(max_usd_per_day=1), "price_table"),
        (lambda d: d["learning"]["patterns"].update(max_calls_per_run=9, max_calls_per_day=3), "max_calls_per_run must not exceed"),
        (lambda d: d["learning"]["patterns"].update(timezone="Mars/Olympus"), "unknown timezone"),
        (lambda d: d["learning"]["patterns"]["egress"].update(pseudonymise=["Tenant!"]) if "egress" in d["learning"]["patterns"] else d["learning"]["patterns"].update(egress={"pseudonymise": ["Tenant!"]}), "attribute name"),
    ],
)
def test_learning_configuration_is_validated(tmp_path, change, message):
    data = config_data(tmp_path, library_dir(tmp_path / "patterns"))
    change(data)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_relative_paths_resolve_and_the_reply_schema_fits_every_provider(tmp_path):
    config, _, _ = setup(tmp_path)
    loaded = load_config(config)
    assert loaded.learning.patterns.spend_ledger == (tmp_path / "state" / "spend.sqlite").resolve()
    from su_multi_arm.learning.patterns import PatternProposalReply
    from su_multi_arm.pipeline.validate import output_format_role_problems, strict_schema_problems
    from su_multi_arm.providers.base import output_format_problems

    assert output_format_problems(PatternProposalReply.model_json_schema(), "PatternProposalReply") == []
    assert strict_schema_problems("openai", "gpt-4o-mini", "native", "pattern_learner") == []
    assert output_format_role_problems("anthropic", "claude-haiku-4-5", "pattern_learner") == []
