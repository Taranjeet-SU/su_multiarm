"""Synthetic checks for biased streams, duplicate leakage and explicitly unavailable holdouts."""
from su_multi_arm.learning.patterns.bootstrap import LogLine
from su_multi_arm.learning.patterns.sampling import balanced_sample, contrast_sample, split_validation


def row(i, text, tenant="client-a", day="2026-01-01"):
    return LogLine(str(i), text, {"tenant": tenant, "date": day, "service": "worker"})


def test_noisy_first_client_cannot_fill_prompt_with_duplicates():
    noisy = [row(i, "request failed after 10 seconds") for i in range(100)]
    rare = [row(100, "request failed after 20 seconds", "client-b", "2026-01-02"),
            row(101, "connection closed before request", "client-c", "2026-01-03")]
    chosen = balanced_sample(noisy + rare, 3)
    assert {line.text for line in chosen.lines} == {line.text for line in noisy + rare}
    assert chosen.summary() == {"input": 102, "unique_texts": 3, "selected": 3, "tenants": 3, "days": 3, "variants": 2}
    assert [line.id for line in chosen.lines] == [line.id for line in balanced_sample(list(reversed(noisy + rare)), 3).lines]


def test_balancing_covers_rare_client_and_day_before_more_common_variants():
    rows = [row(i, f"request failed after {i} seconds") for i in range(30)]
    rows += [row(31, "connection reset before reply", "client-b", "2026-01-02")]
    chosen = balanced_sample(rows, 2)
    assert {line.attributes["tenant"] for line in chosen.lines} == {"client-a", "client-b"}


def test_holdout_latest_day_is_not_mined_and_exact_text_cannot_leak():
    rows = [row(i, f"request failed after {i} seconds") for i in range(4)]
    rows += [row(10, "repeated across days"), row(11, "repeated across days", day="2026-01-02"),
             row(12, "request failed after 99 seconds", day="2026-01-02")]
    split = split_validation(rows)
    assert split.axis == "date" and split.excluded_overlap == 1
    assert {line.attributes["date"] for line in split.validation} == {"2026-01-02"}
    assert not {line.text for line in split.training} & {line.text for line in split.validation}
    assert len(split.training) == 4


def test_tenant_holdout_and_unavailable_are_explicit():
    rows = [row(i, f"failure {i}") for i in range(4)] + [row(5, "rare failure", "client-b")]
    assert split_validation(rows).axis == "tenant"
    unavailable = split_validation(rows[:3])
    assert unavailable.axis is None and unavailable.validation == []


def test_deduplication_does_not_normalize_distinct_message_values():
    rows = [row(1, "wait 1 second"), row(2, "wait 2 seconds"), row(3, "wait 1 second")]
    assert balanced_sample(rows, 8).unique_texts == 2


def test_contrasts_are_real_same_scope_near_misses_and_never_duplicate_positives():
    cluster = [row(1, "request failed after 1 second")]
    other = [row(2, "request succeeded after 1 second"), row(3, cluster[0].text),
             LogLine("4", "request failed after 2 seconds", {"service": "another"})]
    assert [line.id for line in contrast_sample(cluster, other, 2)] == ["2"]
