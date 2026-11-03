"""Span-anchored grounding and predicate support (Phase 0 measured-failure fixes)."""

from __future__ import annotations

import random

import pytest

from su_multi_arm.config import Config, GroundingConfig
from su_multi_arm.grounding import locate, normalise
from su_multi_arm.models import GroundedRelation, Span
from su_multi_arm.predicate_support import PredicateSupport

STEPS = ["nfkc", "casefold", "whitespace"]


@pytest.mark.parametrize(
    "category, source, quote, level",
    [
        ("exact", "The AACTA award was given.", "The AACTA award", "exact"),
        ("case-only", "The AACTA award was given.", "the AACTA award", "normalised"),
        ("whitespace-only", "Austria  at the\n1988 Olympics", "Austria at the 1988 Olympics", "normalised"),
        ("nbsp", "Austria at the 1988", "Austria at the 1988", "normalised"),
        ("ligature", "the ﬁle was lost", "the file was", "normalised"),
        ("full-width", "see ＡＢＣ now", "see ABC now", "normalised"),
        ("combining-marks", "café open", "café open", "exact"),
        ("combining-vs-composed", "café open", "café open", "normalised"),
        ("sharp-s", "Straße 5", "STRASSE 5", "normalised"),
    ],
)
def test_locate_levels_return_verbatim_source_spans(category, source, quote, level):
    located = locate(quote, source, STEPS)
    assert located is not None, category
    assert located.match_level == level
    assert source[located.start : located.end] == located.text
    assert normalise(located.text, STEPS)[0].strip() == normalise(quote, STEPS)[0].strip()


def test_no_match_and_empty_quote_fail_closed():
    assert locate("fabricated", "a real passage", STEPS) is None
    assert locate("   ", "a real passage", STEPS) is None
    assert locate("passage", "", STEPS) is None


def test_ambiguous_policy():
    source = "Alpha uses Beta. Alpha uses Beta."
    assert locate("Alpha uses Beta", source, STEPS, "reject") is None
    first = locate("alpha uses beta", source, STEPS, "first")
    assert first is not None and first.start == 0 and first.occurrences == 2
    exact = locate("Alpha uses Beta", source, STEPS, "first")
    assert exact is not None and exact.match_level == "exact" and exact.occurrences == 2


def test_normalised_match_never_starts_inside_an_expanded_group():
    # "ile" occurs in the normalised "file" but would begin inside the ligature; it must not match there.
    assert locate("ile", "ﬁle", STEPS) is None
    assert locate("file", "ﬁle", STEPS).text == "ﬁle"


def test_no_normalisation_steps_means_exact_only():
    assert locate("the aacta", "The AACTA award", []) is None
    assert locate("The AACTA", "The AACTA award", []).match_level == "exact"


def test_offset_map_property_over_random_sources():
    alphabet = "aB  ﬁßＡé.\n"
    rng = random.Random(7)
    for _ in range(500):
        source = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 24)))
        norm, starts, ends = normalise(source, STEPS)
        assert len(norm) == len(starts) == len(ends)
        assert all(0 <= s < e <= len(source) for s, e in zip(starts, ends))
        assert starts == sorted(starts)
        # Any substring of the source that survives stripping must locate to a verbatim span.
        a = rng.randint(0, len(source) - 1)
        b = rng.randint(a + 1, len(source))
        quote = source[a:b]
        located = locate(quote, source, STEPS, "first")
        if quote.strip():
            assert located is not None
            assert source[located.start : located.end] == located.text


def test_span_model_invariants():
    with pytest.raises(ValueError):
        Span(start=0, end=3, text="ab", match_level="exact")
    with pytest.raises(ValueError):
        GroundedRelation(
            subject="A",
            subject_type="t",
            predicate="uses",
            object="B",
            object_type="t",
            span=Span(start=0, end=6, text="A uses", match_level="exact"),
            subject_span=Span(start=0, end=1, text="A", match_level="exact"),
            object_span=Span(start=7, end=8, text="B", match_level="exact"),
            edge_kind="fact",
            support="trigger_in_span",
        )
    with pytest.raises(ValueError, match="predicate support"):
        GroundedRelation(
            subject="A",
            subject_type="t",
            predicate="uses",
            object="B",
            object_type="t",
            span=Span(start=0, end=8, text="A uses B", match_level="exact"),
            subject_span=Span(start=0, end=1, text="A", match_level="exact"),
            object_span=Span(start=7, end=8, text="B", match_level="exact"),
            edge_kind="fact",
            support="unchecked",
        )


@pytest.mark.parametrize(
    "predicate, span, subject, object_, expected",
    [
        ("USES", "Alpha uses Beta", (0, 5), (11, 15), "trigger_in_span"),
        ("hosted", "Austria at the 1988 Winter Olympics", (0, 7), (15, 35), "unchecked"),
        ("RESOLVED_BY", "the outage was resolved by a restart", (4, 10), (30, 37), "trigger_in_span"),
        ("isLocatedIn", "Plant A is located in Ohio", (0, 7), (22, 26), "trigger_in_span"),
        ("uses", "Alpha does not use Beta", (0, 5), (19, 23), "unchecked"),
        ("uses", "Alpha doesn't use Beta", (0, 5), (18, 22), "unchecked"),
        ("uses", "Alpha uses Beta, not Gamma", (0, 5), (11, 15), "trigger_in_span"),
        ("uses", "", (0, 0), (0, 0), "unchecked"),
    ],
)
def test_predicate_support(predicate, span, subject, object_, expected):
    assert PredicateSupport().check(predicate, span, subject, object_) == expected


def test_declared_triggers_and_language():
    support = PredicateSupport(triggers={"hosted": ["held in", "venue"]})
    assert support.check("hosted", "the Games were held in Calgary", (4, 9), (23, 30)) == "trigger_in_span"
    assert support.check("hosted", "the Games in Calgary", (4, 9), (13, 20)) == "unchecked"
    with pytest.raises(ValueError, match="stemmer_language"):
        Config(extraction={"stemmer_language": "klingon"})
    assert GroundingConfig().ambiguous == "reject"


def _llm(monkeypatch, config, extraction):
    """v2 engine: the extractor role is configured and the raw reply is the model's JSON text."""
    from su_multi_arm.core.config import ChatRole, validate_config
    from su_multi_arm.providers import JsonModel

    data = config.model_dump(mode="json")
    data["models"]["roles"]["extractor"] = ChatRole(provider="openai_compatible", model="fake").model_dump(mode="json")
    data["extraction"]["extractors"] = ["llm"]
    monkeypatch.setattr(JsonModel, "call_text", lambda *args: extraction.model_dump_json())
    return validate_config(data)


def _catalog(engine):
    manifest = engine.manifest()
    return engine.write_path.registry.snapshot_dir(manifest["generation"]) / "catalog.sqlite"


def test_engine_grounds_drifted_quotes_to_verbatim_spans(config, monkeypatch):
    from sqlite3 import connect

    from su_multi_arm import Document, Engine
    from su_multi_arm.models import Entity, Extraction, Relation

    # The model lower-cased "The" and collapsed the line break: v1 aborted the whole build here.
    config = _llm(
        monkeypatch,
        config,
        Extraction(
            entities=[Entity(name="AACTA", type="org", quote="the AACTA"), Entity(name="Film", type="work", quote="film")],
            relations=[Relation(subject="AACTA", predicate="awarded", object="Film", quote="the AACTA awarded the film")],
        ),
    )
    engine = Engine(config)
    engine.ingest([Document(id="d1", dataset="demo", fields={"body": "The AACTA awarded\nthe Film."})])
    with connect(_catalog(engine)) as db:
        rows = db.execute("SELECT quote,start,end,match_level FROM entities ORDER BY start, end").fetchall()
        # entity quotes plus the relation's endpoint mention ("AACTA" inside the relation span)
        assert rows == [("The AACTA", 0, 9, "normalised"), ("AACTA", 4, 9, "exact"), ("Film", 22, 26, "normalised")]
        relation = db.execute("SELECT quote,match_level,edge_kind,support FROM relations").fetchone()
        assert relation == ("The AACTA awarded\nthe Film", "normalised", "fact", "trigger_in_span")
    assert engine.manifest()["index_format"] == 3


def test_unsupported_predicate_is_co_occurrence_and_not_traversed(config, monkeypatch):
    from su_multi_arm import Document, Engine
    from su_multi_arm.models import Entity, Extraction, Relation

    # The public-data false edge: both endpoints sit in a title that does not evidence "hosted".
    title = "Austria at the 1988 Winter Olympics"
    config = _llm(
        monkeypatch,
        config,
        Extraction(
            entities=[
                Entity(name="Austria", type="country", quote="Austria"),
                Entity(name="1988 Winter Olympics", type="event", quote="1988 Winter Olympics"),
            ],
            relations=[Relation(subject="Austria", predicate="hosted", object="1988 Winter Olympics", quote=title)],
        ),
    )
    engine = Engine(config)
    engine.ingest([Document(id=f"row{i}", dataset="demo", fields={"body": title}) for i in range(5)])
    manifest = engine.manifest()
    # Five rows inheriting the same title text collapse to one co-occurrence edge with five occurrences.
    assert manifest["relations"] == 0 and manifest["co_occurrence_relations"] == 1
    assert manifest["co_occurrence_occurrences"] == 5
    graph = engine.search("1988 Winter Olympics").arm_hits["graph"]
    assert graph and all(p["depth"] == 0 for h in graph for p in h.provenance), "co-occurrence edges must not be traversed"


def test_ambiguous_relation_quote_rejected_unless_configured(config, monkeypatch):
    from su_multi_arm import Document, Engine
    from su_multi_arm.models import Entity, Extraction, Relation

    config = _llm(
        monkeypatch,
        config,
        Extraction(
            entities=[Entity(name="Alpha", type="product", quote="Alpha"), Entity(name="Beta", type="part", quote="Beta")],
            relations=[Relation(subject="Alpha", predicate="uses", object="Beta", quote="Alpha uses Beta")],
        ),
    )
    doc = Document(id="dup", dataset="demo", fields={"body": "Alpha uses Beta. Alpha uses Beta."})
    with pytest.raises(RuntimeError, match="not grounded"):
        Engine(config).ingest([doc])
    config.extraction.grounding.ambiguous = "first"
    engine = Engine(config)
    engine.ingest([doc])
    assert engine.manifest()["relations"] == 1
