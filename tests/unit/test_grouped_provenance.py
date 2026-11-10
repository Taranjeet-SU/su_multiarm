"""Grouping permits only proven equal dimensions and explicitly example-only provenance."""
import pytest

from tests.contract.test_grouped_log_ingestion import source


@pytest.mark.parametrize("name,value", [("host", "host-a"), ("container", "container-a"), ("file", "/synthetic/app.log")])
def test_optional_dimension_must_be_explicitly_keyed(name, value):
    unit = source()
    record = unit.records[0]
    setattr(record, name, value)
    with pytest.raises(ValueError, match="unkeyed"):
        unit.validate_grouped()
    unit.grouped[record.id].key_fields.append(name)
    unit.validate_grouped()
    assert getattr(unit.records[0], name) == value


def test_offset_is_retained_only_as_representative_provenance():
    unit = source()
    record = unit.records[0]
    record.offset = 123
    with pytest.raises(ValueError, match="unsupported"):
        unit.validate_grouped()
    unit.grouped[record.id].exemplar_only_fields = ["offset"]
    unit.validate_grouped()
    restored = type(unit).model_validate(unit.model_dump())
    assert restored.records[0].offset == 123
    assert restored.grouped[record.id].exemplar_only_fields == ["offset"]


@pytest.mark.parametrize("fields", [["stack"], ["offset", "offset"], ["offset", "host"]])
def test_other_example_only_fields_cannot_bypass_uniformity(fields):
    unit = source()
    unit.grouped[unit.records[0].id].exemplar_only_fields = fields
    with pytest.raises(ValueError, match="representative-only"):
        unit.validate_grouped()


@pytest.mark.parametrize("count", [False, True, 0, -1, 1.5, "7"])
def test_mutation_cannot_bypass_strict_group_count_validation(count):
    unit = source()
    unit.grouped[unit.records[0].id].occurrence_count = count
    with pytest.raises((ValueError, TypeError)):
        unit.validate_grouped()
