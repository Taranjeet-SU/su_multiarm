"""Safe redaction engine (addendum A1 §3.2.3, slice B11): named built-in redactors, user redactors
on the pattern engine's validator and runtime, the stamp, idempotence, fail-closed timeouts, the
config contract (legacy default, fingerprints unchanged) and the content-free leak scan. Every
value is synthetic (``example.invalid`` addresses, documentation IP ranges, made-up tokens)."""

from __future__ import annotations

import gzip
import json
import sqlite3

import pytest
from pydantic import ValidationError

from su_multi_arm.core.config import REDACTOR_NAMES, PrivacyConfig
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.extraction.patterns.engine import PatternTimeout, re2_available, validate_regex
from su_multi_arm.privacy import BUILTIN, RedactionFailed, SafeRedactor, builtin_redactors, safe_redactor, scan_paths
from tests.pattern_fixtures import engine_or_skip

ENGINES = ["regex"] + (["re2"] if re2_available() else [])


def redactor(names=REDACTOR_NAMES, engine="regex", **privacy):
    engine_or_skip(engine)  # the regex engine is the patterns-lite extra; skipped, never substituted
    return safe_redactor(PrivacyConfig(engine="safe", redactors=list(names), regex_engine=engine, **privacy))


CASES = [
    ("email", "mail from jane.doe+ops@example.invalid failed", "mail from [REDACTED_EMAIL] failed"),
    ("ipv4", "peer 192.0.2.17 closed", "peer [REDACTED_IPV4] closed"),
    ("ipv6", "peer 2001:db8::17 closed", "peer [REDACTED_IPV6] closed"),
    ("ipv6", "peer 2001:0db8:0000:0000:0000:ff00:0042:8329 closed", "peer [REDACTED_IPV6] closed"),
    ("bearer_token", "Authorization: Bearer abcDEF123456.xyz", "Authorization: Bearer [REDACTED_BEARER_TOKEN]"),
    ("jwt", "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl here", "token [REDACTED_JWT] here"),
    ("api_key", "api_key=sYnTh3t1cKey password: hunter2x", "api_key=[REDACTED_API_KEY] password: [REDACTED_API_KEY]"),  # gitleaks:allow (synthetic redactor fixture)
    ("api_key", "using sk-SYNTHETICsyntheticSYNTHETIC0", "using [REDACTED_API_KEY]"),
    ("aws_key", "key AKIAABCDEFGHIJKLMNOP used", "key [REDACTED_AWS_KEY] used"),  # gitleaks:allow (synthetic redactor fixture)
    ("url_credentials", "connect postgres://svc:pa55word@db.invalid:5432/app", "connect postgres://[REDACTED_URL_CREDENTIALS]@db.invalid:5432/app"),
    ("query_values", "GET /search?q=secret+term&page=2 200", "GET /search?q=[REDACTED_QUERY_VALUES]&page=[REDACTED_QUERY_VALUES] 200"),
    ("hex_secret", "digest 0123456789abcdef0123456789abcdef end", "digest [REDACTED_HEX_SECRET] end"),
]


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize(("name", "text", "expected"), CASES)
def test_each_builtin_redacts_its_shape(engine, name, text, expected):
    safe = redactor([name], engine)
    assert safe(text) == expected
    assert safe.counts[name] >= 1


@pytest.mark.parametrize("engine", ENGINES)
def test_ids_are_hashed_stably_and_keyed(engine, monkeypatch):
    safe = redactor(["uuid", "request_id"], engine)
    text = "job 123e4567-e89b-12d3-a456-426614174000 request_id=req-000123"
    out = safe(text)
    assert "123e4567" not in out and "req-000123" not in out
    assert out.startswith("job [UUID:") and "request_id=[REQUEST_ID:" in out
    assert safe(text) == out  # same value, same token: joinable
    monkeypatch.setenv("SU_TEST_REDACT_KEY", "synthetic-key")
    keyed = redactor(["uuid"], engine, hash_key_env="SU_TEST_REDACT_KEY")
    plain = redactor(["uuid"], engine)
    assert keyed(text) != plain(text) and keyed.stamp != plain.stamp


def test_clock_times_and_versions_are_not_ipv6():
    safe = redactor(["ipv6"])
    for text in ("at 12:30:45 done", "2026-01-05T10:15:30Z start", "build abc:def", "cafe deadbeef"):
        assert safe(text) == text


@pytest.mark.parametrize("engine", ENGINES)
def test_redaction_is_idempotent_and_leaves_no_finding(engine):
    safe = redactor(REDACTOR_NAMES, engine)
    text = " ".join(t for _, t, _ in CASES) + " request_id=abcdef123 id 123e4567-e89b-12d3-a456-426614174000"
    once = safe(text)
    assert safe(once) == once
    assert safe.findings(once) == []


def test_every_builtin_is_in_the_safe_subset():
    assert set(BUILTIN) == set(REDACTOR_NAMES)
    for item in BUILTIN.values():
        for regex in item.regexes:
            assert [i for i in validate_regex(regex) if i.severity == "error"] == [], (item.name, regex)


def test_custom_and_legacy_patterns_run_on_the_safe_engine():
    safe = redactor(["email"], custom=[{"name": "ticket", "regex": r"\bTKT-[0-9]{4,8}\b", "action": "hash"}], patterns={"CARD": r"\b[0-9]{4}-[0-9]{4}\b"})
    out = safe("TKT-12345 card 1234-5678 for a@b.example.invalid")
    assert out.startswith("[TICKET:") and "card [REDACTED_CARD]" in out and "[REDACTED_EMAIL]" in out
    assert safe.names == ["email", "ticket", "card"]


@pytest.mark.parametrize("regex", [r"(a+)+$", r"\S+\S+x", r"(?<=x)y", r"(\w+\s?)*$"])
def test_unsafe_user_redactors_are_refused_at_load(regex):
    with pytest.raises((ValidationError, ConfigError), match="outside the safe engine"):
        PrivacyConfig(engine="safe", custom=[{"name": "bad", "regex": regex}])


def test_config_contract():
    assert PrivacyConfig().recipe() == {"redact_emails": False, "patterns": {}, "metadata_fields": []}  # a1/a2 dump: fingerprints unchanged
    assert PrivacyConfig(engine="safe", redactors=["email"]).recipe()["redactors"] == ["email"]
    with pytest.raises(ValidationError, match="need privacy.engine: safe"):
        PrivacyConfig(redactors=["email"])
    with pytest.raises(ValidationError, match="at least one redactor"):
        PrivacyConfig(engine="safe")
    with pytest.raises(ValidationError, match="unique"):
        PrivacyConfig(engine="safe", redactors=["email"], custom=[{"name": "email", "regex": r"x[0-9]{1,3}y"}])
    assert safe_redactor(PrivacyConfig()) is None


def test_the_stamp_names_the_recipe():
    a, b = redactor(["email"]), redactor(["email", "ipv4"])
    assert a.stamp.startswith("safe:") and a.stamp != b.stamp and a.stamp == redactor(["email"]).stamp
    assert a.report() == {"engine": "safe", "stamp": a.stamp, "regex_engine": "regex", "counts": [["email", 0]]}


def test_an_engine_timeout_fails_the_redaction_never_passes_text():
    safe = redactor(["email"])

    def slow(compiled, text):
        raise PatternTimeout("synthetic timeout")

    safe.engine.finditer = slow
    with pytest.raises(RedactionFailed):
        safe("a@b.example.invalid")


def test_a_missing_engine_is_typed_never_substituted(monkeypatch):
    if re2_available():
        pytest.skip("google-re2 is installed")
    from su_multi_arm.extraction.patterns.engine import PatternEngineUnavailable

    with pytest.raises(PatternEngineUnavailable):
        safe_redactor(PrivacyConfig(engine="safe", redactors=["email"], regex_engine="re2"))


def test_leak_scan_reports_locations_not_content(tmp_path):
    (tmp_path / "plain.txt").write_text("clean line\nmail to x@y.example.invalid\n")
    (tmp_path / "rows.json.gz").write_bytes(gzip.compress(json.dumps({"ip": "198.51.100.4"}).encode()))
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01binary")
    with sqlite3.connect(tmp_path / "state.sqlite") as db:
        db.execute("CREATE TABLE docs (id TEXT, body TEXT)")
        db.execute("INSERT INTO docs VALUES ('d1', 'reach me at z@w.example.invalid'), ('d2', '[REDACTED_EMAIL]')")
    report = scan_paths([tmp_path], redactor(["email", "ipv4"])).to_dict()
    assert report["hits"] == 3 and report["by_redactor"] == {"email": 2, "ipv4": 1}
    locations = {(f["path"], f["location"]) for f in report["findings"]}
    assert locations == {("plain.txt", "line 2"), ("rows.json.gz", "line 1"), ("state.sqlite", "docs.body#1")}
    assert report["skipped"] == ["blob.bin: binary"]
    assert "example.invalid" not in json.dumps(report) and "198.51" not in json.dumps(report)


def test_builtin_redactors_keep_the_fixed_order():
    assert [r.name for r in builtin_redactors(["ipv4", "email", "url_credentials"])] == ["url_credentials", "email", "ipv4"]
    with pytest.raises(ValueError):
        builtin_redactors(["nope"])
    with pytest.raises(ValueError):
        SafeRedactor([], None)  # type: ignore[arg-type]


# --- payload leak classes from error-heavy services (synthetic lines) -----------------------------

PAYLOAD_CASES = [
    ("llm_text", "calling model prompt=Summarise the refund policy for order 77", "calling model prompt=<llm_text>"),
    ("llm_text", 'reply completion="The policy says: no." tokens=12', 'reply completion="<llm_text>" tokens=12'),
    ("llm_text", '{"messages": [{"role": "user", "content": "reset \\"my\\" token"}], "temperature": 0}', '{"messages": <llm_text>, "temperature": 0}'),
    ("content", 'hits [{"_id": "d1", "_source": {"title": "Plan", "body": "internal text"}, "_score": 1.2}]', 'hits [{"_id": "d1", "_source": <content>, "_score": 1.2}]'),
    ("content", "rag passage: the contract renews in May", "rag passage: <content>"),
    ("query", 'search query="cheap flights to oslo" took 12ms', 'search query="<query>" took 12ms'),
    ("query", "didyoumean original_query=reciept suggestion=receipt", "didyoumean original_query=<query> suggestion=<query>"),
    (
        "record_payload",
        '{"legal_name": "Example Holdings LLC", "tax_id": "12-3456789", "address": {"city": "Springfield"}, "status": "active"}',
        '{"legal_name": "<record>", "tax_id": "<record>", "address": <record>, "status": "active"}',
    ),
    ("pg_row", "DETAIL:  Key (email, tenant)=(someone at example, t1) already exists.", "DETAIL:  Key (email, tenant)=(<value>) already exists."),
    ("pg_row", "DETAIL: Failing row contains (17, null, O'Brien (jr), 2026-01-01).", "DETAIL: Failing row contains (<value>)."),
    ("pg_row", "DETAIL:  parameters: $1 = 'Jane O''Hara', $2 = '42', $3 = NULL", "DETAIL:  parameters: $1 = '<value>', $2 = '<value>', $3 = <value>"),
]


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize(("name", "text", "expected"), PAYLOAD_CASES)
def test_payload_redactors(engine, name, text, expected):
    safe = redactor([name], engine)
    out = safe(text)
    assert out == expected
    assert safe(out) == out and safe.findings(out) == []


@pytest.mark.parametrize("engine", ENGINES)
def test_tracking_ids_and_cookies_are_hashed(engine):
    safe = redactor(["tracking_id"], engine)
    cookie = safe("Cookie: _ga=GA1.2.3333.4444; sessionid=abc123def456")
    assert cookie.startswith("Cookie: [TRACKING_ID:") and "GA1" not in cookie and "abc123" not in cookie
    visit = safe("visitor session_id=s3ss10nVALUE987 landed")
    assert visit.startswith("visitor session_id=[TRACKING_ID:") and visit.endswith(" landed")


def test_query_hash_action_and_record_field_lists():
    hashed = redactor(["query"], actions={"query": "hash"})
    one, two = hashed("q=alpha"), hashed("q=alpha")
    assert one == two and one.startswith("q=[QUERY:")
    narrowed = redactor(["record_payload"], record_fields_allow=["city"])
    assert narrowed('{"name": "Ann", "city": "Springfield"}') == '{"name": "<record>", "city": "Springfield"}'
    custom = redactor(["record_payload"], record_fields=["customer_ref"])
    assert custom('{"name": "Ann", "customer_ref": "C-1"}') == '{"name": "Ann", "customer_ref": "<record>"}'
    with pytest.raises(ValidationError, match="not enabled"):
        PrivacyConfig(engine="safe", redactors=["email"], actions={"query": "hash"})


def test_the_leak_scan_finds_payload_classes(tmp_path):
    (tmp_path / "app.log").write_text('ok\nllm prompt=hello there\n{"_source": {"body": "x"}}\nDETAIL:  Key (id)=(7) already exists.\n')
    report = scan_paths([tmp_path], redactor(["llm_text", "content", "pg_row"])).to_dict()
    assert report["by_redactor"] == {"content": 1, "llm_text": 1, "pg_row": 1}
