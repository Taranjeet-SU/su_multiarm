"""L0 fixtures (addendum A1 §4): the synthetic log generator is deterministic and covers the three
line styles, multiline fragments, health-check noise and synthetic PII shapes; the ReDoS corpus is
well-formed. Nothing here is derived from a real system."""

from __future__ import annotations

import re
import time
from datetime import date

import pytest

from su_multi_arm.testing.fixtures.logs import SyntheticLogSpec, generate_day, redos_cases
from su_multi_arm.testing.fixtures.logs.generator import STYLE

SPEC = SyntheticLogSpec(day=date(2026, 1, 5), lines_per_index=200)


def test_generator_is_deterministic_and_seed_sensitive():
    a, b = generate_day(SPEC), generate_day(SPEC)
    assert a.digest() == b.digest() and a.indices == b.indices
    other = generate_day(SyntheticLogSpec(day=date(2026, 1, 5), lines_per_index=200, seed=8))
    assert other.digest() != a.digest()
    next_day = generate_day(SyntheticLogSpec(day=date(2026, 1, 6), lines_per_index=200))
    assert set(next_day.indices).isdisjoint(a.indices)


def test_generator_digest_is_pinned():
    """Byte-level determinism across runs and platforms (update deliberately with the generator)."""
    small = generate_day(SyntheticLogSpec(day=date(2026, 1, 5), lines_per_index=20, tenants=("t001",)))
    assert small.digest() == PINNED


PINNED = "71a0224f0f566a5544d9f2415b4b9fe4c2b8e71ae22ea58bc1f9c7844c83569c"


def test_line_styles_noise_multiline_and_fragments_are_present():
    day = generate_day(SPEC)
    by_style: dict[str, list[str]] = {}
    for name, docs in day.indices.items():
        service = name.split("-", 2)[2].rsplit("-", 1)[0]
        by_style.setdefault(STYLE[service], []).extend(d["_source"]["message"] for d in docs)
    assert any(re.match(r"^\[\d{4}-\d{2}-\d{2}T[\d:.]+Z\] \[ERROR\] \[[a-z-]+\] ", m) for m in by_style["node"])
    assert any(m.startswith("\x1b[31m[") and "[ERROR]" in m and "\x1b[0m" in m for m in by_style["python_ansi"])
    assert any(re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} ERROR \[main-\d\] org\.example\.", m) for m in by_style["log4j"])
    assert any(m.startswith("\tat ") for m in by_style["log4j"]) and any(m.startswith("Caused by: ") for m in by_style["log4j"])
    assert any(m.startswith("Traceback (most recent call last):") for m in by_style["python_ansi"])
    assert any(m.lstrip().startswith("at Handler.") for m in by_style["node"])
    all_messages = [m for ms in by_style.values() for m in ms]
    assert any("GET /healthz 200" in m for m in all_messages)
    for name, truth in day.truth.items():
        assert truth.docs == len(day.indices[name])
        assert truth.fragments == 1 and truth.noise > 0 and truth.multiline_heads > 0
        assert truth.docs == sum(truth.by_level.values()) + truth.continuation_docs + truth.fragments


def test_level_field_only_in_node_style_indices():
    day = generate_day(SPEC)
    for name, mapping in day.mappings.items():
        has_level = "level" in mapping["mappings"]["properties"]
        assert has_level == ("query-gateway" in name or "scheduler" in name)


def test_pii_shapes_are_synthetic_only():
    day = generate_day(SPEC)
    emails = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
    ips = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b")
    found = 0
    for docs in day.indices.values():
        for d in docs:
            for m in emails.finditer(d["_source"]["message"]):
                assert m.group().endswith("@example.invalid")
                found += 1
            for m in ips.finditer(d["_source"]["message"]):
                assert m.group().startswith("192.0.2.")
    assert found == sum(t.pii_lines for t in day.truth.values()) > 0
    clean = generate_day(SyntheticLogSpec(day=date(2026, 1, 5), lines_per_index=200, pii_shapes=False))
    assert not any("@example.invalid" in d["_source"]["message"] for docs in clean.indices.values() for d in docs)


def test_redos_corpus_is_well_formed():
    cases = redos_cases()
    assert len(cases) >= 8 and len({c.id for c in cases}) == len(cases)
    assert {c.cls for c in cases} >= {"nested_quantifier", "overlapping_alternation", "polynomial", "backreference", "lookaround"}
    for case in cases:
        re.compile(case.pattern)  # Python re compiles all of them; RE2 refuses the re2: false ones
        assert (case.expect == "refuse") == (not case.re2)
        assert len(case.attack()) >= case.repeat


@pytest.mark.parametrize("case", [c for c in redos_cases() if c.id == "nested_plus"], ids=lambda c: c.id)
def test_redos_attack_grows_superlinearly_under_backtracking(case):
    """Evidence that the corpus bites: Python ``re`` time roughly doubles per extra pump for the
    nested quantifier. Small sizes only (milliseconds)."""
    pattern = re.compile(case.pattern)

    def timed(n):
        start = time.perf_counter()
        pattern.match(case.attack(n))
        return time.perf_counter() - start

    small, large = min(timed(14) for _ in range(3)), min(timed(18) for _ in range(3))
    assert large > small * 4
