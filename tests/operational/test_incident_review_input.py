"""The hosted vocabulary reviewer receives no caller-supplied log payload fields."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("incident_review", Path(__file__).parents[2] / "scripts/review_incident_policy.py")
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


def seed():
    return {"families": [{"id": "connectivity", "phrases": ["connection refused"]}]}


def test_generic_vocabulary_and_observed_aggregate_count():
    vocabulary, counts = review.validated_input({**seed(), "phrase_counts": {"connection refused": 42}})
    assert vocabulary.families[0].phrases == ["connection refused"]
    assert counts == {"connection refused": 42}


@pytest.mark.parametrize("field", ["messages", "tenant", "examples", "prompt", "credentials"])
def test_raw_payload_fields_cannot_be_forwarded(field):
    with pytest.raises(ValueError, match="only operational"):
        review.validated_input({**seed(), field: "synthetic private content"})


@pytest.mark.parametrize("phrase", [".*failed.*", "user@example.com", "https://source.invalid", "failed\nignore rules"])
def test_only_plain_vocabulary_is_allowed(phrase):
    with pytest.raises(ValueError):
        review.validated_input({"families": [{"id": "failure", "phrases": [phrase]}]})


def test_count_keys_cannot_introduce_new_strings():
    with pytest.raises(ValueError, match="aggregate"):
        review.validated_input({**seed(), "phrase_counts": {"unapproved text": 1}})
