"""Pattern engine, validator and ReDoS lint (addendum §3.4.1): RE2 subset, ReDoS rules, explicit engine
selection without fallback, the RE2 Set prefilter, the regex engine's ASCII semantics and timeouts."""

from __future__ import annotations

import sys
import time

import pytest

from su_multi_arm.extraction.patterns.engine import (
    PatternEngineUnavailable,
    PatternTimeout,
    RegexEngine,
    analyse,
    re2_available,
    select_engine,
    validate_regex,
)
from tests.pattern_fixtures import CONNECT, FORMAT, engine_or_skip

ENGINES = ["re2", "regex"]


def codes(pattern: str, **kwargs) -> set[str]:
    return {i.code for i in validate_regex(pattern, **kwargs) if i.severity == "error"}


# Each entry: a pattern a backtracking engine can take exponential or polynomial time on, and the
# rule that refuses it. Kept small and synthetic (the L0 corpus may extend it).
REDOS_CORPUS = [
    (r"^(a+)+$", "star_height"),
    (r"^(?:\w+\s?)*$", "star_height"),
    (r"^(?:a*)*b$", "star_height"),
    (r"^(?:[a-z]+){2,}$", "star_height"),
    (r"^(?:a|ab)+c$", "quantified_alternation"),
    (r"^(?:ab|a)+c$", "quantified_alternation"),
    (r"^(?:x|x)*y$", None),  # the stdlib parser folds ``x|x`` into a class: no alternation left
    (r"^\S+\S+$", "adjacent_unbounded"),
    (r"^.*.*=$", "adjacent_unbounded"),
    (r"^.*x?.*$", "adjacent_unbounded"),
    (r"^a.* b$", "unbounded_follow"),
    (r"error (?P<code>[0-9]+)", "unbounded_follow"),
    (r"^(?P<k>[a-z]+)[a-z]$", "unbounded_follow"),
]


@pytest.mark.parametrize(("pattern", "rule"), REDOS_CORPUS)
def test_redos_corpus(pattern, rule):
    found = codes(pattern)
    if rule is None:
        assert "star_height" not in found
    else:
        assert rule in found


@pytest.mark.parametrize(
    "pattern",
    [
        FORMAT["regex"],
        CONNECT["regex"],
        r"^v=(?P<n>-?[0-9]+(?:\.[0-9]+)?)$",
        r'^key="(?P<v>[^"]*)" end$',
        r"^(?:ab){2,3}$",
        r"(?i)^conn (?P<x>[a-z]+)$",
        r"^(?P<a>\S+) +(?P<b>\S+)$",
        r"^\w+\b",
    ],
)
def test_safe_patterns_pass(pattern):
    assert codes(pattern) == set()


@pytest.mark.parametrize(
    ("pattern", "needle"),
    [
        (r"(?P<a>a)(?P=a)", "backreference"),
        (r"(?=a)b$", "lookahead"),
        (r"(?<!a)b$", "lookahead"),
        (r"(?>ab)c$", "atomic"),
        (r"^a++$", "possessive"),
        (r"^ab\Z", "\\Z"),
        (r"^a{,3}$", "{,n}"),
        (r"(?x)^a b$", "flags"),
        (r"^\p{L}$", "\\p"),
        (r"^a{1001}$", "1000"),
    ],
)
def test_outside_the_re2_subset_is_refused(pattern, needle):
    issues = [i for i in validate_regex(pattern) if i.severity == "error"]
    assert issues and any(i.code in {"re2_subset", "syntax", "re2_compile"} for i in issues)


def test_unnamed_groups_and_newlines_are_refused():
    assert "unnamed_group" in codes(r"^(a)b$")
    assert "syntax" in codes("^a\nb$")


def test_warnings_for_few_literals_and_unanchored():
    issues = validate_regex(r"(?P<a>[a-z]+)-(?P<b>[0-9]+)\b", min_literal_chars=4)
    assert {i.code for i in issues if i.severity == "warning"} == {"few_literals", "unanchored"}
    assert analyse(r"^abc (?P<x>[0-9]+)$").literal_chars == 4


def test_missing_engine_is_an_error_never_a_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, "re2", None)  # import re2 -> ImportError
    with pytest.raises(PatternEngineUnavailable, match="google-re2"):
        select_engine("re2")
    assert not re2_available()
    monkeypatch.setitem(sys.modules, "regex", None)
    with pytest.raises(PatternEngineUnavailable, match="regex"):
        select_engine("regex")


@pytest.mark.parametrize("kind", ENGINES)
def test_spans_are_character_offsets_and_ascii_classes(kind):
    engine = engine_or_skip(kind)
    compiled = engine.compile(r"(?P<n>[0-9]+) (?P<w>\w+)")
    hit = engine.search(compiled, "héllo wörld 123 ab")
    assert hit is not None and hit.groups["n"] == (12, 15) and hit.groups["w"] == (16, 18)
    # \w is ASCII under both engines: the accented word is not one token
    assert engine.search(engine.compile(r"^(?P<w>\w+)$"), "wörld") is None
    optional = engine.search(engine.compile(r"(?P<a>x)?y"), "y")
    assert optional.groups["a"] is None


def test_re2_set_prefilter_selects_candidates():
    if not re2_available():
        pytest.skip("google-re2 not installed")
    engine = select_engine("re2")
    prefilter = engine.build_set([r"connect (?P<d>[a-z]+)", r"^cache miss", r"timeout"])
    assert engine.prefilter(prefilter, "db-client connect redis", 3) == {0}
    assert engine.prefilter(prefilter, "nothing here", 3) == set()
    assert engine.build_set([]) is None and engine.prefilter(None, "x", 0) == set()


def test_regex_engine_has_no_set_and_flags_nondeterminism():
    engine = engine_or_skip("regex")
    assert engine.prefilter(None, "anything", 3) == {0, 1, 2}
    assert engine.identity()["engine"] == "regex" and engine.deterministic is False


def test_regex_engine_timeout_is_typed():
    pytest.importorskip("regex")
    engine = RegexEngine(timeout_ms=1)
    seen = {}

    class Slow:  # the regex module raises TimeoutError past ``timeout``; checked as a mapping
        groupindex: dict = {}

        def search(self, text, timeout=None):
            seen["timeout"] = timeout
            raise TimeoutError("regex timed out")

    with pytest.raises(PatternTimeout):
        engine.search(Slow(), "a" * 40 + "b")
    assert seen["timeout"] == 0.001


def test_re2_is_linear_on_the_classic_redos_input():
    if not re2_available():
        pytest.skip("google-re2 not installed")
    engine = select_engine("re2")
    compiled = engine.compile(r"(?P<a>a+)+$")
    started = time.perf_counter()
    assert engine.search(compiled, "a" * 5000 + "b") is None
    assert time.perf_counter() - started < 0.5
