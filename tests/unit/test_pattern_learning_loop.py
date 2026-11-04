"""LLM pattern-learning loop (addendum A1 §3.6.1–3.6.3, slice L4b) with a scripted fake provider:
egress refusal, each gate rejection, auto vs manual, the CAS retry under a concurrent human save,
``library.yaml`` byte-identical, budget refusal, determinism, dry run, human decisions and git.
Mocked providers only (``httpx.MockTransport``): no network, no key, no spend. Synthetic data."""

from __future__ import annotations

import functools
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from su_multi_arm.core.errors import ConfigError
from su_multi_arm.extraction.patterns import writer
from su_multi_arm.extraction.patterns.engine import select_engine
from su_multi_arm.extraction.patterns.library import load_library
from su_multi_arm.learning.patterns import (
    DecisionRefused,
    EgressRefused,
    LineRedactor,
    human_decision,
    session_from_config,
)
from su_multi_arm.learning.patterns.egress import EgressPolicy, lines_from_records
from tests.pattern_fixtures import engine_or_skip
from tests.pattern_learning_support import (
    CACHE_RE,
    HOSTED_ROLE,
    NOW,
    SESSION_RE,
    FakeLearner,
    by_template,
    clock,
    learning_lines,
    library_dir,
    line,
    make_config,
    reply,
)


@pytest.fixture(params=["regex", "re2"])
def kind(request):
    engine_or_skip(request.param)
    return request.param


def run(tmp_path: Path, *, script=by_template, lines=None, extra=None, dry_run=False, mode="learn", **config):
    library = tmp_path / "patterns"
    if not library.exists():
        library_dir(library, extra)
    cfg = make_config(tmp_path, library, **config)
    fake = FakeLearner(script)
    session = session_from_config(cfg, dry_run=dry_run, transport=fake.transport(), clock=clock)
    report = session.run(learning_lines() if lines is None else lines, mode=mode, dry_run=dry_run, now=NOW)
    return report, fake, library


def rows(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text()) if path.exists() else []


def outcome(report, size: int):
    return next(c for c in report.clusters if c.size == size)


# --- egress -----------------------------------------------------------------------------------


def test_egress_refuses_lines_the_configured_redaction_did_not_process(tmp_path):
    library = library_dir(tmp_path / "patterns")
    cfg = make_config(tmp_path, library)
    fake = FakeLearner()
    session = session_from_config(cfg, transport=fake.transport(), clock=clock)
    from su_multi_arm.learning.patterns import learn

    common = dict(load=session.load, ref=session.ref, settings=session.settings, egress=session.egress, role=session.role, proposer=session.proposer, meter=session.meter)
    with pytest.raises(EgressRefused, match="not processed by the configured redaction"):
        learn(learning_lines(), **common)  # plain LogLines: never redacted
    other = LineRedactor(cfg.privacy.model_copy(update={"redact_emails": False}))
    with pytest.raises(EgressRefused, match="another privacy configuration"):
        learn(other.redact_all(learning_lines()), **common)
    assert fake.requests == [] and not (library / "proposed.yaml").exists()


def test_egress_refuses_a_residual_pii_shape_rather_than_dropping_the_line(tmp_path):
    engine_or_skip("regex")
    lines = learning_lines() + [line(90, "warn", "cache miss for key 10.0.0.1 in 3 ms")]
    with pytest.raises(EgressRefused, match="l090: unredacted ipv4"):
        run(tmp_path, lines=lines, privacy={"redact_emails": True})  # no IP redaction configured


def test_a_config_without_redaction_cannot_send_anything(tmp_path):
    with pytest.raises(EgressRefused, match="no redaction is configured"):
        run(tmp_path, privacy={})


def test_hosted_models_need_allow_hosted(tmp_path, monkeypatch):
    monkeypatch.setenv("SU_TEST_FAKE_KEY", "sk-offline-not-a-key")
    with pytest.raises(ConfigError, match="allow_hosted"):
        make_config(tmp_path, library_dir(tmp_path / "patterns"), role=HOSTED_ROLE, price_table=True, learning={"max_usd_per_run": 1, "max_usd_per_day": 2})
    policy = EgressPolicy(make_config(tmp_path, tmp_path / "patterns").learning.patterns.egress, LineRedactor(make_config(tmp_path, tmp_path / "patterns").privacy))
    from su_multi_arm.core.config import ChatRole

    with pytest.raises(EgressRefused, match="allow_hosted is false"):
        policy.check_role(ChatRole.model_validate(HOSTED_ROLE))


def test_redacted_and_pseudonymised_text_only_leaves_the_process(tmp_path):
    engine_or_skip("regex")
    # A cluster whose lines name their own host and tenant and carry an e-mail address and an IP.
    lines = learning_lines() + [
        line(70 + i, "warn", f"notify ops.person@example.org about node-alpha-01 of tenant-alpha at 192.0.2.1{i}") for i in range(4)
    ]
    report, fake, _ = run(tmp_path, lines=lines, promotion="manual")
    sent = fake.sent_text()
    assert fake.requests and "ops.person@example.org" not in sent and "192.0.2." not in sent
    assert "node-alpha-01" not in sent and "tenant-alpha" not in sent and "host-1" in sent
    assert "[REDACTED_EMAIL]" in sent and "[REDACTED_IPV4]" in sent
    assert report.egress["redactions"] >= 8 and report.egress["pseudonymised"] >= 2


def test_records_from_an_unredacted_mapping_are_refused():
    class Mapping:
        redaction = "none"

    with pytest.raises(EgressRefused, match="no redaction"):
        lines_from_records([], Mapping())


# --- gate rejections through the loop -----------------------------------------------------------

STEAL = r"^(?:cache miss for key (?P<key>[0-9a-f\-]+) in (?P<ms>[0-9]+) ms|(?P<c>[a-z]+-client) failed to connect (?P<d>[a-z]+) after (?P<n>[0-9]+) ms)$"


@pytest.mark.parametrize(
    ("make", "failed"),
    [
        (lambda p: reply(p, positives=[*[r["id"] for r in p["lines"][:2]], "l999"]), "cited_examples"),  # invented id
        (lambda p: reply(p, negatives=[p["lines"][3]["id"]]), "lint"),  # a cited negative matches its own pattern
        (lambda p: reply(p, regex=r"^cache miss for (?P<rest>[^\"]+)$"), "out_of_cluster"),  # over-broad
        (lambda p: reply(p, regex=r"^cache miss for key (?P<key>[0-9a-f\-]+) in (?P<ms>[0-9]) ms$"), "cluster_coverage"),
        (lambda p: reply(p, regex=STEAL), "no_regressions"),  # steals the approved pattern's lines
        (lambda p: reply(p, regex=r"^cache (?P<a>\S+)\S+ ms$"), "lint"),  # ReDoS lint
        (lambda p: reply(p, regex=r"^(?P<a>[a-z]+)$"), "lint"),  # fewer literal characters than the minimum
        (lambda p: reply(p, positives=[p["lines"][0]["id"], p["lines"][1]["id"]]), "min_support"),
        (lambda p: reply(p, regex=r"^cache miss for key (?P<k>[0-9a-f\-]+) in (?P<ms>[0-9]+) ms (?P=k)$"), ("re2_compile", "lint")),  # backreference
    ],
)
def test_each_gate_rejection_writes_nothing(tmp_path, kind, make, failed):
    def script(payload):
        return make(payload) if payload["template"].startswith("cache miss for key") else reply(payload, regex=None, action="none")

    user_bytes = None
    report, fake, library = run(tmp_path, script=script, engine=kind)
    user_bytes = (library / "library.yaml").read_bytes()
    cache = outcome(report, 12)
    expected = failed if isinstance(failed, tuple) else (failed,)
    assert cache.outcome == "rejected" and any(name in cache.reason for name in expected), cache.reason
    assert not (library / "learned.yaml").exists() and not (library / "proposed.yaml").exists() and not (library / "decisions.yaml").exists()
    assert report.after.coverage == report.before.coverage and (library / "library.yaml").read_bytes() == user_bytes


def test_a_duplicate_of_a_disabled_pattern_fails_novelty(tmp_path, kind):
    """A human disable wins: re-proposing the disabled regex is rejected (novelty), and a fix of it is invalid."""
    disabled = {
        "id": "llm.old",
        "kind": "message",
        "status": "approved",
        "priority": 1,
        "regex": CACHE_RE,
        "signature": {"id": "llm.old"},
        "applies_to": {"service": ["nowhere"]},
        "provenance": {"author": "system:pattern_learner"},
    }
    library = tmp_path / "patterns"
    library_dir(library)
    (library / "learned.yaml").write_text(yaml.safe_dump([disabled]))
    (library / "decisions.yaml").write_text(yaml.safe_dump([{"op": "disable", "id": "llm.old", "by": "user:owner", "at": "2026-11-04T09:00:00+00:00"}]))
    report, _, _ = run(tmp_path, engine=kind)
    assert outcome(report, 12).outcome == "rejected" and "novelty" in outcome(report, 12).reason

    def fix(payload):
        return reply(payload, action="fix", target_pattern_id="llm.old")

    report, _, _ = run(tmp_path, script=fix, engine=kind)
    assert outcome(report, 12).outcome == "invalid" and "disabled" in outcome(report, 12).reason


def test_invalid_replies_are_recorded_not_repaired(tmp_path):
    engine_or_skip("regex")
    def script(payload):
        if payload["template"].startswith("cache miss for key"):
            return reply(payload, cluster_id="c-other")
        if payload["template"].startswith("session"):
            return '{"cluster_id": "x"}'  # schema violation
        return reply(payload, regex=None, action="new")

    report, fake, library = run(tmp_path, script=script)
    assert [c.outcome for c in report.clusters] == ["invalid", "invalid_reply", "invalid"]
    assert len(fake.requests) == 3 and not (library / "proposed.yaml").exists() and report.stopped is None


# --- promotion policy ---------------------------------------------------------------------------


def test_manual_policy_keeps_passing_proposals_in_proposed_yaml(tmp_path, kind):
    report, _, library = run(tmp_path, engine=kind, promotion="manual")
    user_bytes = (library / "library.yaml").read_bytes()
    assert [c.outcome for c in report.clusters] == ["proposed", "proposed", "declined"]
    assert len(rows(library / "proposed.yaml")) == 2 and not (library / "learned.yaml").exists() and not (library / "decisions.yaml").exists()
    assert report.after.coverage == report.before.coverage == 0.16
    assert report.after_if_approved.coverage == pytest.approx(22 / 25)
    proposal = rows(library / "proposed.yaml")[0]
    assert proposal["status"] == "proposed" and proposal["priority"] == 49 and proposal["provenance"]["issue_name"] == "cache miss"
    assert (library / "library.yaml").read_bytes() == user_bytes


def test_auto_policy_learns_with_a_decision_record_and_reparses(tmp_path):
    engine_or_skip("re2")
    report, _, library = run(tmp_path, engine="re2", promotion="auto")
    assert [c.outcome for c in report.clusters] == ["learned", "learned", "declined"]
    learned = rows(library / "learned.yaml")
    decisions = rows(library / "decisions.yaml")
    assert [r["status"] for r in learned] == ["approved", "approved"] and [d["op"] for d in decisions] == ["approve", "approve"]
    assert all(d["by"] == "system:pattern_learner" and len(d["examples"]) == 3 for d in decisions)
    assert report.before.coverage == 0.16 and report.after.coverage == pytest.approx(22 / 25)
    assert not (library / "proposed.yaml").exists()


def test_auto_on_the_regex_engine_still_waits_for_a_human(tmp_path):
    """Auto-promotion needs a gate run on RE2: on the development engine the proposal is not promoted."""
    engine_or_skip("regex")
    report, _, library = run(tmp_path, engine="regex", promotion="auto")
    assert outcome(report, 12).outcome == "proposed" and "RE2" in outcome(report, 12).reason
    assert not (library / "learned.yaml").exists()


LOGIN_RE = r"^user login rejected on (?P<node>\S+) for (?P<account>\S+) account$"


def login_lines() -> list:
    """Bodies that carry the line's own host and tenant values (one in another letter case)."""
    out = []
    for i in range(4):
        tenant = "tenant-alpha" if i % 2 else "tenant-beta"
        shown = tenant.upper() if i == 0 else tenant
        out.append(line(80 + i, "warn", f"user login rejected on node-alpha-01 for {shown} account", tenant=tenant))
    return out


def login_script(regex: str):
    def script(payload):
        if payload["template"].startswith("user login"):
            return reply(payload, regex=regex, issue_name="login rejected", issue_category="authentication", fields=[])
        return by_template(payload)

    return script


@pytest.mark.parametrize("promotion", ["manual", "auto"])
def test_stored_examples_are_pseudonymised_like_egress(tmp_path, kind, promotion):
    """Real-run fix: ``examples.positive`` of learned and proposed entries (and the decision's copy)
    kept tenant and host values. They are pseudonymised like the prompt text: case-insensitive and
    on token boundaries."""
    report, _, library = run(tmp_path, engine=kind, promotion=promotion, lines=learning_lines() + login_lines(), script=login_script(LOGIN_RE))
    stored = [*rows(library / "proposed.yaml"), *rows(library / "learned.yaml")]
    login = next(r for r in stored if r["regex"] == LOGIN_RE)
    texts = [*login["examples"]["positive"], *(t for d in rows(library / "decisions.yaml") for t in d.get("examples", []))]
    assert login["examples"]["positive"] and all("host-1" in t for t in login["examples"]["positive"])
    joined = "\n".join(texts).casefold()
    assert "node-alpha-01" not in joined and "tenant-alpha" not in joined and "tenant-beta" not in joined
    assert all(r["examples"]["positive"] for r in stored)  # the library still loads: examples match
    load_library(library, select_engine(kind))


def test_examples_that_stop_matching_after_pseudonymisation_are_dropped_and_counted(tmp_path, kind):
    """A regex narrower than the aliases (``node-[a-z]+-[0-9]+`` does not match ``host-1``): those
    examples are left out (counted in provenance), never written with the real value; with no
    positive example left the entry is not written."""
    narrow = r"^user login rejected on (?P<node>node-[a-z]+-[0-9]+) for (?P<account>\S+) account$"
    report, _, library = run(tmp_path, engine=kind, promotion="manual", lines=learning_lines() + login_lines(), script=login_script(narrow))
    stored = [*rows(library / "proposed.yaml"), *rows(library / "learned.yaml")]
    assert not any(r["regex"] == narrow for r in stored)
    assert any(c.outcome == "rejected" and c.reason == "no positive example matches after pseudonymisation" for c in report.clusters)
    assert "node-alpha-01" not in (library / "proposed.yaml").read_text()


def test_propose_mode_never_promotes(tmp_path, kind):
    report, _, library = run(tmp_path, engine=kind, promotion="auto", mode="propose")
    assert report.policy == "manual" and report.written()["learned"] == [] and len(rows(library / "proposed.yaml")) == 2


def test_a_fix_of_a_learned_pattern_supersedes_it_and_library_yaml_is_untouched(tmp_path):
    engine_or_skip("re2")
    narrow = {
        "id": "llm.narrow",
        "kind": "message",
        "status": "approved",
        "priority": 10,
        "regex": r"^session (?P<sid>[0-9a-f\-]+) expired after (?P<s>6[0-2]) seconds idle$",
        "signature": {"id": "session.expired", "category": "lifecycle"},
        "examples": {"positive": ["session 00000001-0000-4000-8000-000000000001 expired after 60 seconds idle"]},
        "provenance": {"author": "system:pattern_learner"},
    }
    library = tmp_path / "patterns"
    library_dir(library)
    (library / "learned.yaml").write_text(yaml.safe_dump([narrow]))
    user_bytes = (library / "library.yaml").read_bytes()

    def script(payload):
        if payload["template"].startswith("session"):
            return reply(payload, regex=SESSION_RE, action="fix", target_pattern_id="llm.narrow", fields=[{"group": "s", "type": "integer"}])
        return by_template(payload)

    report, _, _ = run(tmp_path, script=script, engine="re2", promotion="auto")
    fix = next(c for c in report.clusters if c.target == "llm.narrow")
    assert fix.outcome == "learned" and fix.gate["checks"]["fix_target"]["passed"]
    supersede = [d for d in rows(library / "decisions.yaml") if d["op"] == "supersede"]
    assert supersede == [{**supersede[0], "id": fix.entry_id, "target": "llm.narrow"}]
    loaded = load_library(library, select_engine("re2"))
    assert loaded.entries["llm.narrow"].status == "superseded" and loaded.entries[fix.entry_id].entry.signature.id == "session.expired"
    assert (library / "library.yaml").read_bytes() == user_bytes


def test_a_fix_of_a_human_pattern_waits_and_a_human_promote_supersedes_it(tmp_path, kind):
    def script(payload):
        if payload["template"].startswith("cache miss for key"):
            # The fix keeps every positive example of the target and adds the cache lines.
            regex = r"^(?:(?P<client>[a-z]+-client) (?P<trigger>failed to connect|refused by) (?P<dep>kafka|mongo|postgres|redis)(?: after (?P<ms>[0-9]+) ?ms)?|cache miss for key (?P<key>[0-9a-f\-]+) in (?P<cms>[0-9]+) ms)$"
            return reply(payload, regex=regex, action="fix", target_pattern_id="msg.dependency.connect_failed")
        return reply(payload, regex=None, action="none")

    report, _, library = run(tmp_path, script=script, engine=kind, promotion="auto")
    user_bytes = (library / "library.yaml").read_bytes()
    fix = outcome(report, 12)
    assert fix.outcome == "proposed" and fix.target == "msg.dependency.connect_failed", fix.reason
    loaded = load_library(library, select_engine(kind))
    decision = human_decision(library, "promote", fix.entry_id, by="user:owner", library=loaded, now=NOW)
    assert decision.op == "supersede" and decision.target == "msg.dependency.connect_failed"
    loaded = load_library(library, select_engine(kind))
    assert loaded.entries["msg.dependency.connect_failed"].status == "superseded" and loaded.entries[fix.entry_id].status == "approved"
    assert (library / "library.yaml").read_bytes() == user_bytes


# --- human decisions ------------------------------------------------------------------------------


def test_human_promote_deprecate_disable(tmp_path, kind):
    _, _, library = run(tmp_path, engine=kind, promotion="manual")
    engine = select_engine(kind)
    first = rows(library / "proposed.yaml")[0]["id"]
    human_decision(library, "promote", first, by="user:owner", library=load_library(library, engine), now=NOW)
    loaded = load_library(library, engine)
    assert loaded.entries[first].status == "approved"
    with pytest.raises(DecisionRefused, match="only a proposed or deprecated"):
        human_decision(library, "promote", first, by="user:owner", library=loaded)
    human_decision(library, "deprecate", first, by="user:owner", library=loaded, now=NOW)
    assert load_library(library, engine).entries[first].status == "deprecated"
    human_decision(library, "disable", first, by="user:owner", library=load_library(library, engine), now=NOW)
    # A later system record cannot revive a disabled entry.
    writer.append_decision(library, __import__("su_multi_arm.extraction.patterns.library", fromlist=["Decision"]).Decision(op="approve", id=first, by="system:auto", at=NOW))
    assert load_library(library, engine).entries[first].status == "disabled"
    with pytest.raises(DecisionRefused, match="user:<name>"):
        human_decision(library, "disable", first, by="system:auto", library=load_library(library, engine))
    with pytest.raises(DecisionRefused, match="not in the library"):
        human_decision(library, "promote", "llm.nope", by="user:owner", library=load_library(library, engine))


# --- CAS and determinism --------------------------------------------------------------------------


def test_a_concurrent_human_save_between_read_and_rename_is_retried(tmp_path, monkeypatch):
    engine_or_skip("regex")
    library = library_dir(tmp_path / "patterns")
    (library / "proposed.yaml").write_text("- {id: hand.placeholder, kind: noise, status: proposed, regex: '^placeholder line$'}\n")
    saves = []

    def human_save():
        if not saves:
            with open(library / "proposed.yaml", "a") as handle:
                handle.write("- {id: hand.saved, kind: noise, status: proposed, regex: '^hand saved line$'}\n")
            saves.append(1)

    monkeypatch.setattr(writer, "append_rows", functools.partial(writer.append_rows, _before_replace=human_save))
    report, _, _ = run(tmp_path, promotion="manual")
    ids = [r["id"] for r in rows(library / "proposed.yaml")]
    assert saves == [1] and ids[:2] == ["hand.placeholder", "hand.saved"] and len(ids) == 4  # nothing lost
    assert report.written()["proposed"] == ids[2:]


def test_a_fixed_mock_gives_byte_identical_files_and_reports(tmp_path, kind):
    outputs = []
    for name in ("one", "two"):
        root = tmp_path / name
        root.mkdir()
        report, _, library = run(root, engine=kind, promotion="manual")
        data = report.to_dict()
        data["spend"]["day"].pop("scope")
        outputs.append((data, (library / "proposed.yaml").read_bytes()))
    assert outputs[0] == outputs[1]


# --- budget ---------------------------------------------------------------------------------------


def test_the_day_cap_refuses_before_any_request_and_persists_across_runs(tmp_path):
    engine_or_skip("regex")
    report, fake, library = run(tmp_path, learning={"max_calls_per_run": 2, "max_calls_per_day": 3}, promotion="manual")
    assert len(fake.requests) == 2 and report.stopped == "budget_exhausted:calls"  # the run cap
    # Run 2: the two templates proposed in run 1 wait for review (no new call, real-run fix); the
    # remaining cluster takes the day's third and last call.
    report, fake, _ = run(tmp_path, learning={"max_calls_per_run": 2, "max_calls_per_day": 3}, promotion="manual")
    assert len(fake.requests) == 1 and [c.outcome for c in report.clusters].count("pending_proposal") == 2
    # Run 3: the persisted day ledger refuses the declined cluster's call before its request.
    report, fake, _ = run(tmp_path, learning={"max_calls_per_run": 2, "max_calls_per_day": 3}, promotion="manual")
    assert fake.requests == [] and report.stopped == "budget_exhausted:day_calls" and any(c.outcome == "not_sent" for c in report.clusters)
    assert report.spend["day"]["calls"] == 3


def test_usd_caps_refuse_before_the_request(tmp_path):
    engine_or_skip("regex")
    # One call reserves (chars/4 input + 512 output tokens) at $1000 per million: about $1.0-1.15 here,
    # far above $0.10, so nothing is sent.
    report, fake, library = run(tmp_path, price_table=True, learning={"max_usd_per_run": 0.1, "max_usd_per_day": 1.6})
    assert fake.requests == [] and report.stopped == "budget_exhausted:usd" and not (library / "proposed.yaml").exists()
    assert report.spend["day"]["calls"] == 0 and report.spend["day"]["committed_usd"] == 0
    # With $1.60 the first call fits; it commits the reported usage ($0.52), and the next
    # reservation (0.52 + about 1.1 > 1.6) is refused by the run cap before its request.
    report, fake, _ = run(tmp_path, price_table=True, learning={"max_usd_per_run": 1.6, "max_usd_per_day": 1.6})
    assert len(fake.requests) == 1 and report.stopped == "budget_exhausted:usd"
    assert report.spend["day"]["committed_usd"] == pytest.approx(0.52) and report.spend["day"]["reserved_usd"] == 0
    # A new run starts with a fresh run budget, but the persisted day ledger refuses it.
    report, fake, _ = run(tmp_path, price_table=True, learning={"max_usd_per_run": 1.6, "max_usd_per_day": 1.6})
    assert fake.requests == [] and report.stopped == "budget_exhausted:day_usd"


def test_max_usd_flag_only_lowers_the_run_cap(tmp_path):
    library = library_dir(tmp_path / "patterns")
    cfg = make_config(tmp_path, library, price_table=True, learning={"max_usd_per_run": 2, "max_usd_per_day": 5})
    assert session_from_config(cfg, max_usd=50, clock=clock).meter.budget.limits.max_usd == 2
    assert session_from_config(cfg, max_usd=0.5, clock=clock).meter.budget.limits.max_usd == 0.5
    with pytest.raises(ConfigError, match="price_table"):
        session_from_config(make_config(tmp_path, library), max_usd=1)


def test_two_ledgers_on_one_file_never_exceed_the_cap(tmp_path):
    import threading

    from su_multi_arm.core.errors import BudgetExhausted
    from su_multi_arm.learning.patterns import DaySpendLedger

    path = tmp_path / "ledger.sqlite"
    ledgers = [DaySpendLedger(path, max_calls=1000, max_usd=1.0, clock=clock) for _ in range(2)]
    granted = []

    def worker(ledger):
        for _ in range(30):
            try:
                granted.append(ledger.reserve(0.05))
            except BudgetExhausted:
                pass

    threads = [threading.Thread(target=worker, args=(ledger,)) for ledger in ledgers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(granted) == 20 and ledgers[0].usage()["reserved_usd"] == pytest.approx(1.0)


# --- dry run --------------------------------------------------------------------------------------


def test_dry_run_sends_spends_and_writes_nothing(tmp_path):
    engine_or_skip("regex")
    report, fake, library = run(tmp_path, dry_run=True, price_table=True, learning={"max_usd_per_run": 1, "max_usd_per_day": 2})
    assert fake.requests == [] and [c.outcome for c in report.clusters] == ["would_send"] * 3
    assert all(c.estimate_usd and c.prompt_chars for c in report.clusters) and report.spend is None
    assert sorted(p.name for p in library.iterdir()) == ["library.yaml"] and not (tmp_path / "spend.sqlite").exists()


# --- git ------------------------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    library = library_dir(tmp_path / "patterns")
    _git(library, "init", "-q")
    _git(library, "config", "user.email", "owner@example.invalid")
    _git(library, "config", "user.name", "owner")
    _git(library, "add", "library.yaml")
    _git(library, "commit", "-q", "-m", "library")
    return library


def test_git_commit_of_the_system_files(tmp_path, repo):
    engine_or_skip("regex")
    report, _, _ = run(tmp_path, promotion="manual", learning={"git_commit": True})
    assert report.git_commit
    log = _git(repo, "log", "--format=%an|%s", "-1")
    assert log.strip() == "system:pattern_learner|patterns learn: 0 learned, 2 proposed"
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["proposed.yaml"]
    assert _git(repo, "status", "--porcelain", "library.yaml") == ""


def test_a_dirty_system_file_stops_the_writer(tmp_path, repo):
    engine_or_skip("regex")
    run(tmp_path, promotion="manual", learning={"git_commit": True})
    before = (repo / "proposed.yaml").read_bytes() + b"# a hand edit\n"
    (repo / "proposed.yaml").write_bytes(before)
    with pytest.raises(writer.SystemFileDirty):
        run(tmp_path, promotion="manual", learning={"git_commit": True})
    assert (repo / "proposed.yaml").read_bytes() == before


def test_a_reply_using_library_masks_is_expanded_for_the_gate_and_the_reparse(tmp_path, kind):
    def script(payload):
        if payload["template"].startswith("cache miss for key"):
            return reply(payload, regex=r"^cache miss for key (?P<key>{{mask:uuid}}) in (?P<ms>{{mask:num}}) ms$")
        return reply(payload, regex=None, action="none")

    report, _, library = run(tmp_path, script=script, engine=kind, promotion="manual")
    assert outcome(report, 12).outcome == "proposed"
    assert report.after_if_approved.coverage == pytest.approx(16 / 25)
    assert "{{mask:uuid}}" in rows(library / "proposed.yaml")[0]["regex"]  # stored as written; expanded at load
