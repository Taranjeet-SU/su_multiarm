"""More named redactors of the safe engine (B11; real-deployment coverage gaps): ``phone``,
``person_name``, ``long_numeric_id``, ``record_list`` and ``product_prose``. All are off unless
listed in ``privacy.redactors`` (like every built-in). Every value is synthetic (documentation phone
ranges such as 555-01xx, invented names and products)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from su_multi_arm.core.config import PrivacyConfig
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.extraction.patterns.engine import re2_available
from su_multi_arm.privacy import safe_redactor
from tests.pattern_fixtures import engine_or_skip

ENGINES = ["regex"] + (["re2"] if re2_available() else [])


def redactor(names, engine="regex", **privacy):
    engine_or_skip(engine)
    return safe_redactor(PrivacyConfig(engine="safe", redactors=list(names), regex_engine=engine, **privacy))


@pytest.fixture(params=ENGINES)
def engine(request):
    return request.param


# --- phone --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("call +1 415 555 0100 now", "call [REDACTED_PHONE] now"),
        ("uk +44 20 7946 0958.", "uk [REDACTED_PHONE]."),
        ("de +49-30-555-01234", "de [REDACTED_PHONE]"),
        ("in +91 98765 43210", "in [REDACTED_PHONE]"),
        ("fr +33 1 23 45 67 89", "fr [REDACTED_PHONE]"),
        ("us +1 (415) 555-0100", "us [REDACTED_PHONE]"),
        ("dial (415) 555-0100 please", "dial [REDACTED_PHONE] please"),
        ("phone=0301234567 ok", "phone=[REDACTED_PHONE] ok"),
        ('{"mobile": "030-555-0123"}', '{"mobile": "[REDACTED_PHONE]"}'),
    ],
)
def test_phone_numbers_in_international_and_keyed_formats(engine, text, expected):
    assert redactor(["phone"], engine)(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "at 2026-03-05T14:00:00+01:00 done",  # a timezone offset
        "ts 14:00:00 +0100 ok",  # too few digits after +
        "version 1.2.3 build 4567",
        "took 1234 ms for 56789 rows",
        "+12345678901234567890",  # more than 15 digits: not a phone number
    ],
)
def test_phone_leaves_times_versions_and_counts(engine, text):
    assert redactor(["phone"], engine)(text) == text


# --- person_name --------------------------------------------------------------------------------------


def test_person_name_fields_in_json_and_key_value_text(engine):
    safe = redactor(["person_name"], engine)
    record = '{"firstName": "Jane", "lastName": "Roe-Example", "displayName": "J. Roe", "id": 7, "author": null}'
    out = safe(record)
    assert json.loads(out) == {"firstName": "[REDACTED_PERSON_NAME]", "lastName": "[REDACTED_PERSON_NAME]", "displayName": "[REDACTED_PERSON_NAME]", "id": 7, "author": None}
    assert safe('author=Jane Q Roe action=commit') == "author=[REDACTED_PERSON_NAME] action=commit"
    assert safe("userName: jroe42 logged in") == "userName: [REDACTED_PERSON_NAME] logged in"
    assert safe("full_name='Jane Roe' ok") == "full_name='[REDACTED_PERSON_NAME]' ok"
    assert safe("hostname=node-01 filename=a.txt") == "hostname=node-01 filename=a.txt"  # key boundaries


def test_person_name_key_list_is_configurable(engine):
    safe = redactor(["person_name"], engine, person_name_fields=["assignee"])
    assert safe('{"assignee": "Jane Roe", "name": "svc-api"}') == '{"assignee": "[REDACTED_PERSON_NAME]", "name": "svc-api"}'


# --- long_numeric_id ----------------------------------------------------------------------------------


def test_long_numeric_ids_are_hashed_and_joinable(engine):
    safe = redactor(["long_numeric_id"], engine)
    out = safe("account 4111111111111111 and 1234567890123 and 1234567890123456789")
    assert "4111111111111111" not in out and out.count("[LONG_NUMERIC_ID:") == 3
    assert safe("again 4111111111111111") == "again " + out.split()[1]  # same value, same token
    kept = "short 123456789012 long 12345678901234567890 dotted 1.5"
    assert safe(kept) == kept  # 12 digits and 20 digits are not in the class


# --- record_list --------------------------------------------------------------------------------------


def test_lists_of_business_records_are_masked_whole(engine):
    safe = redactor(["record_list"], engine)
    text = '{"customers": [{"id": 1, "name": "Example Corp"}, {"id": 2, "name": "Sample LLC"}], "tags": ["a", "b"], "total": 2}'
    assert safe(text) == '{"customers": <record>, "tags": ["a", "b"], "total": 2}'
    assert safe('{"items": ["x", "y"]}') == '{"items": ["x", "y"]}'  # not a list of records
    configured = redactor(["record_list"], engine, record_list_fields=["tenants_seen"])
    assert configured('{"tenants_seen": [ {"n": "Example Corp"} ], "customers": [{"n": 1}]}') == '{"tenants_seen": <record>, "customers": [{"n": 1}]}'


# --- product_prose ------------------------------------------------------------------------------------


def test_free_text_after_a_product_name_is_masked_and_names_do_not_stop_it(engine):
    safe = redactor(["product_prose"], engine, prose_products=["Kafka", "Example Search", "Redis"])
    assert safe("feedback: Kafka consumer stuck for Jane at Example Corp") == "feedback: Kafka <prose>"
    # a later allow-listed name stays visible and the masking resumes after it
    assert safe("user said example search is slow, redis also slow for our team") == "user said example search <prose> redis <prose>"
    assert safe("kafkaesque wording stays") == "kafkaesque wording stays"  # token-bounded
    assert safe("Kafka\nnext line stays") == "Kafka\nnext line stays"  # end of the line ends the prose
    out = safe("Kafka retry for Jane")
    assert safe(out) == out  # idempotent


def test_product_prose_needs_its_names_and_names_need_it():
    with pytest.raises((ValidationError, ConfigError), match="prose_products"):
        PrivacyConfig(engine="safe", redactors=["product_prose"])
    with pytest.raises((ValidationError, ConfigError), match="prose_products"):
        PrivacyConfig(engine="safe", redactors=["email"], prose_products=["Kafka"])
    with pytest.raises((ValidationError, ConfigError), match="prose product"):
        PrivacyConfig(engine="safe", redactors=["product_prose"], prose_products=["(bad"])


# --- defaults and identity ----------------------------------------------------------------------------


def test_new_redactors_are_off_by_default_and_keep_existing_fingerprints():
    legacy = PrivacyConfig()
    assert "person_name_fields" not in legacy.recipe() and "prose_products" not in legacy.recipe()
    with pytest.raises((ValidationError, ConfigError), match="need privacy.engine: safe"):
        PrivacyConfig(person_name_fields=["author"])
    safe = PrivacyConfig(engine="safe", redactors=["email"])
    assert set(safe.recipe()) == set(PrivacyConfig(engine="safe", redactors=["email"]).recipe())
    assert not {"phone", "person_name", "long_numeric_id", "record_list", "product_prose"} & set(safe.redactors)


def test_the_stamp_names_the_configured_lists(engine):
    a = redactor(["person_name"], engine)
    b = redactor(["person_name"], engine, person_name_fields=["assignee"])
    c = redactor(["product_prose"], engine, prose_products=["Kafka"])
    d = redactor(["product_prose"], engine, prose_products=["Redis"])
    assert a.stamp != b.stamp and c.stamp != d.stamp
