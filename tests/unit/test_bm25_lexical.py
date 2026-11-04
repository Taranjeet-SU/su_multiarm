"""sqlite ``lexical_engine: bm25``: Okapi BM25 identical to ``rank_bm25.BM25Okapi`` (idf epsilon floor
included), generation-pinned memory-mapped segment, eligibility as a document bitset, deterministic
tie order, recipe/fingerprint behaviour, and the lexical arm's verbatim-window and identifier
front policies. Synthetic corpora only."""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from su_multi_arm import Document, Engine
from su_multi_arm.arms import ArmInput
from su_multi_arm.backends.sqlite import SqliteBackend
from su_multi_arm.core.config import Bm25Settings, SqliteBackend as SqliteBackendConfig, load_config, validate_config
from su_multi_arm.core.errors import BackendError
from su_multi_arm.core.types import Chunk, GenerationId, LexicalQuery, Page, Predicate, Scope
from su_multi_arm.extraction.base import BuiltinExtractor
from su_multi_arm.ingestion.write import WritePath
from su_multi_arm.selection.collapse import collapse_arm
from su_multi_arm.selection.types import Candidate

GEN = GenerationId(dataset="bm25", generation="a" * 32, index_format=3, fingerprint="f" * 64)
GEN2 = GEN.model_copy(update={"generation": "b" * 32})
ROOT = Path(__file__).resolve().parents[2]


def build(tmp_path, texts: dict[str, str], gen=GEN, settings: Bm25Settings | None = None, metadata=None, titles=None, name="s"):
    backend = SqliteBackend(tmp_path / name, lexical_engine="bm25", bm25=settings or Bm25Settings())
    documents, chunks = [], []
    for cid, text in texts.items():
        doc_id = "d" + cid[1:]
        documents.append(Document(id=doc_id, dataset="bm25", title=(titles or {}).get(doc_id, ""), metadata=(metadata or {}).get(doc_id, {})))
        chunks.append(Chunk(id=cid, document_id=doc_id, field="body", start=0, end=len(text), text=text))
    backend.catalog.put_documents(gen, documents, chunks)
    backend.lexical.index(gen, chunks, [])
    backend.lexical.refresh(gen)
    return backend


def synthetic_corpus(n=240, seed=7) -> dict[str, str]:
    """Zipf-like vocabulary: a few words occur in most rows (negative idf -> epsilon floor), one word
    in exactly half of the rows (idf exactly 0), and a long tail of rare words."""
    rng = random.Random(seed)
    vocab = [f"w{i}" for i in range(300)]
    weights = [1.0 / (i + 1) ** 1.1 for i in range(len(vocab))]
    texts = {}
    for i in range(n):
        words = rng.choices(vocab, weights=weights, k=rng.randint(0, 40))
        if i % 2 == 0:
            words.append("half")
        if rng.random() < 0.3:
            words += ["Error", "CODE-42", "état"]
        texts[f"c{i:04d}"] = " ".join(words)
    return texts


def test_scores_equal_rank_bm25_bm25okapi_bit_for_bit(tmp_path):
    rank_bm25 = pytest.importorskip("rank_bm25")
    texts = synthetic_corpus()
    settings = Bm25Settings(tokenizer="ascii_alnum")
    backend = build(tmp_path, texts, settings=settings)
    lexical = backend.lexical
    analyze = lexical.analyze
    corpus = [analyze(texts[cid]) for cid in sorted(texts)]  # rows are in chunk-id order
    reference = rank_bm25.BM25Okapi(corpus)
    segment = lexical.segment(GEN)
    assert segment.corpus_size == len(corpus) and segment.avgdl == reference.avgdl
    assert segment.meta["average_idf"] == reference.average_idf
    found = lexical.term_ids(GEN, list(reference.idf))
    assert all(segment.idf[found[w][0]] == v for w, v in reference.idf.items())  # epsilon floor included
    negative = [w for w in reference.idf if reference.idf[w] == 0.25 * reference.average_idf]
    assert negative, "the corpus must exercise the epsilon floor"
    queries = ["w0 w1 w5", "w3 w3 w17 half", "error code 42 etat", "w250 unknownword w2", "half", "w0 w0 w0", "w299 w298 w1"]
    for text in queries:
        tokens = analyze(text)
        expected = reference.get_scores(tokens)
        got, matched = segment.scores(found[t][0] if t in found else None for t in tokens)
        assert np.array_equal(got, expected), text  # bit-identical
        assert np.max(np.abs(got - expected)) <= 1e-9
        contains = np.array([any(t in set(row) for t in tokens) for row in corpus])
        assert np.array_equal(matched, contains)
        # ranked list: every row with a query token, by (-score, chunk id)
        page = lexical.search(GEN, LexicalQuery(text=text), None, 10000)
        order = sorted(np.flatnonzero(contains), key=lambda r: (-expected[r], r))
        assert [h.chunk_id for h in page.items] == [sorted(texts)[r] for r in order]
        assert [h.score for h in page.items] == [float(expected[r]) for r in order]


def test_a_token_in_exactly_half_the_rows_has_idf_zero_and_ranks_last(tmp_path):
    backend = build(tmp_path, {"c1": "alpha beta", "c2": "alpha gamma", "c3": "delta", "c4": "epsilon"})
    page = backend.lexical.search(GEN, LexicalQuery(text="alpha gamma"), None, 10)
    assert [h.chunk_id for h in page.items] == ["c2", "c1"] and page.items[1].score == 0.0


def test_ties_rank_by_chunk_id_whatever_the_ingest_order(tmp_path):
    texts = {"c3": "same words here", "c1": "same words here", "c2": "same words here", "c9": "other"}
    backend = build(tmp_path, texts)
    reversed_backend = build(tmp_path, dict(reversed(list(texts.items()))), name="r")
    for b in (backend, reversed_backend):
        assert [h.chunk_id for h in b.lexical.search(GEN, LexicalQuery(text="words"), None, 10).items] == ["c1", "c2", "c3"]
    paged = backend.lexical.search(GEN, LexicalQuery(text="words"), None, 10, Page(size=2))
    rest = backend.lexical.search(GEN, LexicalQuery(text="words"), None, 10, Page(cursor=paged.next_cursor, size=5))
    assert [h.chunk_id for h in [*paged.items, *rest.items]] == ["c1", "c2", "c3"] and rest.next_cursor is None


def test_eligibility_is_pushed_down_as_a_document_bitset(tmp_path):
    texts = {f"c{i}": f"invoice tax {'rate ' * i}" for i in range(1, 7)}
    metadata = {f"d{i}": {"region": "north" if i % 2 else "south"} for i in range(1, 7)}
    backend = build(tmp_path, texts, metadata=metadata)
    search = lambda scope, k=10: [h.document_id for h in backend.lexical.search(GEN, LexicalQuery(text="tax rate"), scope, k).items]  # noqa: E731
    everything = search(None)
    assert sorted(everything) == [f"d{i}" for i in range(1, 7)]
    assert search(Scope(document_ids=["d2", "d5"])) == [d for d in everything if d in {"d2", "d5"}]
    assert search(Scope(exclude_ids=["d1"])) == [d for d in everything if d != "d1"]
    north = search(Scope(predicates=[Predicate(field="region", op="eq", value="north")], exclude_ids=["d3"]))
    assert north == [d for d in everything if d in {"d1", "d5"}]
    assert search(Scope(predicates=[Predicate(field="region", op="eq", value="south")]), k=1) == [next(d for d in everything if d in {"d2", "d4", "d6"})]
    assert search(Scope(document_ids=[])) == []
    assert backend.lexical.search(GEN, LexicalQuery(text="tax", fields=["title"]), None, 10).items == []


def test_segment_is_pinned_per_generation_and_checked_against_the_settings(tmp_path):
    backend = build(tmp_path, {"c1": "alpha one"})
    build(tmp_path, {"c1": "beta two"}, gen=GEN2)  # another backend instance, same root
    other = SqliteBackend(tmp_path / "s", lexical_engine="bm25")
    assert [h.chunk_id for h in other.lexical.search(GEN, LexicalQuery(text="alpha"), None, 5).items] == ["c1"]
    assert other.lexical.search(GEN2, LexicalQuery(text="alpha"), None, 5).items == []
    folder = backend.snapshot_dir(GEN) / "bm25"
    assert {p.name for p in folder.iterdir()} >= {"meta.json", "post_rows.npy", "idf.npy", "chunk_ids.json"}
    assert isinstance(backend.lexical.segment(GEN).post_rows, np.memmap)
    changed = SqliteBackend(tmp_path / "s", lexical_engine="bm25", bm25=Bm25Settings(k1=1.2))
    with pytest.raises(BackendError, match="other analyzer/scoring settings"):
        changed.lexical.search(GEN, LexicalQuery(text="alpha"), None, 5)
    fts = SqliteBackend(tmp_path / "f")
    fts.catalog.put_documents(GEN, [Document(id="d1")], [])
    with pytest.raises(BackendError, match="no bm25 segment"):
        SqliteBackend(tmp_path / "f", lexical_engine="bm25").lexical.search(GEN, LexicalQuery(text="alpha"), None, 5)
    with pytest.raises(BackendError, match="unknown generation"):
        other.lexical.search(GEN.model_copy(update={"generation": "c" * 32}), LexicalQuery(text="alpha"), None, 5)
    backend.catalog.delete_generation(GEN)
    assert not folder.exists()


def test_analyzer_options_stemming_case_unicode_and_title(tmp_path):
    texts = {"c1": "Running the RUNNER runs", "c2": "café état naïve"}
    plain = build(tmp_path, texts, name="plain")
    assert plain.lexical.search(GEN, LexicalQuery(text="run"), None, 5).items == []
    assert [h.chunk_id for h in plain.lexical.search(GEN, LexicalQuery(text="ÉTAT"), None, 5).items] == ["c2"]
    stemmed = build(tmp_path, texts, settings=Bm25Settings(stemming="english"), name="stem")
    assert [h.chunk_id for h in stemmed.lexical.search(GEN, LexicalQuery(text="run"), None, 5).items] == ["c1"]
    ascii_only = build(tmp_path, texts, settings=Bm25Settings(tokenizer="ascii_alnum"), name="ascii")
    assert ascii_only.lexical.analyze("café-état") == ["caf", "tat"]
    keep_case = build(tmp_path, texts, settings=Bm25Settings(lowercase=False), name="case")
    assert keep_case.lexical.search(GEN, LexicalQuery(text="running"), None, 5).items == []
    titled = build(tmp_path, {"c1": "body text"}, settings=Bm25Settings(include_title=True), titles={"d1": "Quarterly filing"}, name="title")
    assert [h.chunk_id for h in titled.lexical.search(GEN, LexicalQuery(text="quarterly"), None, 5).items] == ["c1"]
    assert titled.analyzer_id(GEN) == "bm25_okapi:unicode_word:lower:none:title"
    assert SqliteBackend(tmp_path / "x").analyzer_id(GEN) == "fts5_unicode61"


def test_phrase_boolean_and_phrase_rows_verify_adjacency(tmp_path):
    texts = {"c1": "exemption certificate rounding", "c2": "certificate exemption", "c3": "rounding error exemption certificate"}
    lexical = build(tmp_path, texts).lexical
    phrase = lexical.search(GEN, LexicalQuery(kind="phrase", text="exemption certificate"), None, 10)
    assert sorted(h.chunk_id for h in phrase.items) == ["c1", "c3"]
    boolean = lexical.search(GEN, LexicalQuery(kind="boolean", text="certificate", must=["exemption certificate"], must_not=["rounding error"]), None, 10)
    assert [h.chunk_id for h in boolean.items] == ["c1"]
    assert [r.chunk_id for r in lexical.phrase_rows(GEN, ["exemption", "certificate"], None, 10)] == ["c1", "c3"]
    assert lexical.phrase_rows(GEN, ["exemption", "certificate"], None, 2) is None  # three rows hold both tokens
    assert [r.chunk_id for r in lexical.phrase_rows(GEN, ["exemption", "certificate"], Scope(exclude_ids=["d1"]), 2)] == ["c3"]
    assert lexical.doc_freqs(GEN, ["exemption", "nothing"]) == [3, 0]


def test_repeated_query_tokens_count_again_like_bm25okapi(tmp_path):
    lexical = build(tmp_path, {"c1": "alpha beta", "c2": "beta gamma", "c3": "delta", "c4": "omega"}).lexical
    once = {h.chunk_id: h.score for h in lexical.search(GEN, LexicalQuery(text="alpha beta"), None, 5).items}
    twice = {h.chunk_id: h.score for h in lexical.search(GEN, LexicalQuery(text="alpha alpha beta"), None, 5).items}
    assert twice["c2"] == once["c2"] and twice["c1"] > once["c1"]
    assert lexical.scores_query_repeats is True and SqliteBackend(tmp_path / "x").lexical.scores_query_repeats is False


# --- configuration and recipe ------------------------------------------------------------------


def test_bm25_settings_need_the_engine_and_a_known_stemmer():
    assert SqliteBackendConfig(kind="sqlite", lexical_engine="bm25").bm25 == Bm25Settings()
    with pytest.raises(ValidationError, match="lexical_engine: bm25"):
        SqliteBackendConfig(kind="sqlite", bm25={"k1": 1.2})
    with pytest.raises(ValidationError, match="unknown stemmer"):
        Bm25Settings(stemming="klingon")
    with pytest.raises(ValidationError):
        Bm25Settings(tokenizer="whitespace")


def _fingerprint(config) -> str:
    extractor = BuiltinExtractor(config.extraction, SimpleNamespace(providers={}))
    storage, arms = config.storage, config.pipeline.arms
    slots = {"catalog": storage.catalog, "records": storage.records, "ledger": storage.ledger, "communities": storage.communities}
    if arms.lexical:
        slots["lexical"] = arms.lexical.backend
    return WritePath.index_fingerprint(SimpleNamespace(cfg=config, extractor=extractor, slots=slots))


def test_the_engine_keys_enter_the_index_recipe_only_when_bm25_is_selected():
    fts = SqliteBackendConfig(kind="sqlite")
    assert "lexical_engine" not in fts.recipe() and "bm25" not in fts.recipe()
    assert fts.recipe() == {k: v for k, v in fts.model_dump(mode="json").items() if k not in {"lexical_engine", "bm25"}}
    bm25 = SqliteBackendConfig(kind="sqlite", lexical_engine="bm25")
    assert bm25.recipe()["lexical_engine"] == "bm25" and bm25.recipe()["bm25"]["k1"] == 1.5
    base = load_config(ROOT / "examples/v2/local.yaml", check_plugins=False, warn=False)
    name = base.pipeline.arms.lexical.backend
    assert base.backends[name].kind == "sqlite"
    switched = base.model_copy(deep=True)
    switched.backends[name] = base.backends[name].model_copy(update={"lexical_engine": "bm25", "bm25": Bm25Settings()})
    tuned = base.model_copy(deep=True)
    tuned.backends[name] = base.backends[name].model_copy(update={"lexical_engine": "bm25", "bm25": Bm25Settings(k1=1.2)})
    assert len({_fingerprint(base), _fingerprint(switched), _fingerprint(tuned)}) == 3


# --- engine end to end and arm policies ----------------------------------------------------------

DOCS = [
    Document(id="kb:1", title="Exemption certificates", fields={"body": "Upload the exemption certificate. Error E-1042 means the certificate expired before the invoice date."}, metadata={"number": "KB-0001"}),
    Document(id="kb:2", title="Rounding", fields={"body": "Rounding applies per invoice line for every jurisdiction."}, metadata={"number": "KB-0002"}),
    Document(id="case:3", title="Failed upload", fields={"body": "Customer pasted: the certificate expired before the invoice date so the upload failed."}, metadata={"number": "CS-0003"}),
    Document(id="case:4", title="Unrelated", fields={"body": "Archive retention settings for closed periods."}, metadata={"number": "CS-0004"}),
    Document(id="case:5", title="Non-ASCII only", fields={"body": "日本語 テキスト"}, metadata={"number": "CS-0005"}),
]


def engine_for(tmp_path, lexical=None, engine="bm25", name="state"):
    backend = {"kind": "sqlite", "path": str(tmp_path / name)}
    if engine == "bm25":
        backend.update(lexical_engine="bm25", bm25={"tokenizer": "ascii_alnum"})
    data = {
        "schema_version": 2,
        "dataset": "default",
        "backends": {"local": backend},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": {"fields": [{"name": "number", "type": "id", "filterable": True, "pattern": r"\b(?:KB|CS)-\d{4}\b"}]},
        "pipeline": {"arms": {"lexical": {"backend": "local", "candidates": 50, **(lexical or {})}}},
        "extraction": {"extractors": []},
    }
    built = Engine(validate_config(data))
    built.ingest(DOCS)
    return built


def run_lexical(engine, query, scope=None):
    ctx = engine._context_for(None)
    return engine.arms["lexical"].run(ctx, ArmInput(query=query, scope=scope or Scope(), candidates=50))


def test_engine_ingests_and_searches_with_bm25_and_records_the_analyzer(tmp_path):
    engine = engine_for(tmp_path)  # includes a document without ASCII words: validation samples analyzable words
    result = engine.search("certificate expired invoice")
    assert {h.document_id for h in result.hits} >= {"kb:1", "case:3"}
    snapshot = next((tmp_path / "state" / "snapshots").iterdir())
    recorded = (snapshot / "manifest.json").read_text()
    assert '"analyzer_id": "bm25_okapi:ascii_alnum:lower:none"' in recorded and "fts5_unicode61" not in recorded
    assert (snapshot / "bm25" / "meta.json").exists()


def test_verbatim_boost_front_pins_documents_holding_query_windows(tmp_path):
    query = "why does the certificate expired before the invoice date error appear"
    plain = run_lexical(engine_for(tmp_path, name="a"), query)
    policy = {"verbatim_boost": {"enabled": True, "window_tokens": 4, "min_windows": 2, "common_df": 10}}
    boosted = run_lexical(engine_for(tmp_path, policy, name="b"), query)
    front = [h for h in boosted.hits if any(p.get("lexical_front") for p in h.provenance)]
    assert {h.document_id for h in front} == {"kb:1", "case:3"} and boosted.hits[: len(front)] == front
    assert all(p["lexical_front"] == "verbatim" and p["windows"] >= 2 for h in front for p in h.provenance[:1])
    assert [h.score for h in boosted.hits] == sorted((h.score for h in boosted.hits), reverse=True)
    assert {h.document_id for h in boosted.hits} == {h.document_id for h in plain.hits}
    # a quoted string pins on its own; the DF guard drops windows made only of common tokens
    quoted = run_lexical(engine_for(tmp_path, {"verbatim_boost": {"enabled": True}}, name="c"), 'error "rounding applies per invoice line" now')
    assert quoted.hits[0].document_id == "kb:2" and quoted.hits[0].provenance[0]["lexical_front"] == "quoted"
    guarded = run_lexical(engine_for(tmp_path, {**policy, "verbatim_boost": {**policy["verbatim_boost"], "common_df": 1, "quoted": False}}, name="d"), query)
    assert not any(p.get("lexical_front") for h in guarded.hits for p in h.provenance)
    # scope is respected: an excluded document is never pinned
    excluded = run_lexical(engine_for(tmp_path, policy, name="e"), query, Scope(exclude_ids=["kb:1"]))
    assert "kb:1" not in {h.document_id for h in excluded.hits}


def test_verbatim_boost_needs_an_index_with_term_statistics(tmp_path):
    with pytest.raises(Exception, match="verbatim_boost"):
        engine_for(tmp_path, {"verbatim_boost": {"enabled": True}}, engine="fts5")


def test_identifier_pin_puts_named_documents_first_inside_the_scope(tmp_path):
    engine = engine_for(tmp_path, {"pin_identifiers": {"enabled": True}})
    result = run_lexical(engine, "rounding per invoice line, see CS-0004 and KB-9999")
    assert result.hits[0].document_id == "case:4" and result.hits[0].provenance[0]["lexical_front"] == "identifier"
    assert result.hits[0].chunk_ids == []  # not a term hit: no chunk is claimed
    assert result.hits[1].document_id == "kb:2"
    again = run_lexical(engine, "KB-0002 rounding")
    assert again.hits[0].document_id == "kb:2" and again.hits[0].chunk_ids  # the term hit itself moves first
    assert run_lexical(engine, "rounding CS-0004", Scope(exclude_ids=["case:4"])).hits[0].document_id == "kb:2"
    with pytest.raises(Exception, match="pin_identifiers"):
        engine_for(tmp_path, {"pin_identifiers": {"enabled": True, "fields": ["missing"]}}, name="bad")


def test_policies_are_off_by_default_and_fts5_receives_distinct_terms(tmp_path, monkeypatch):
    engine = engine_for(tmp_path, engine="fts5")
    seen = []
    facet = engine.arms["lexical"]._facet()
    original = facet.search
    monkeypatch.setattr(facet, "search", lambda gen, query, *a, **k: (seen.append(query.text), original(gen, query, *a, **k))[1])
    run_lexical(engine, "Invoice invoice line LINE rounding")
    assert seen == ["Invoice line rounding"]
    cfg = engine.config.pipeline.arms.lexical
    assert not cfg.verbatim_boost.enabled and not cfg.pin_identifiers.enabled and not cfg.phrase_boost.enabled


# --- request-path helpers --------------------------------------------------------------------------


def test_collapse_through_hit_parts_equals_the_document_read_path(tmp_path):
    engine = engine_for(tmp_path)
    arm = engine.arms["lexical"]
    ctx = engine._context_for(None)
    page = engine.backends["local"].lexical.search(ctx.generation, LexicalQuery(text="certificate rounding"), None, 10)
    chunk_ids = [h.chunk_id for h in page.items]
    assert len(chunk_ids) >= 3
    scored = [(chunk_ids[0], 3.0, {"x": 1}), (chunk_ids[1], 2.0, {}), (chunk_ids[0], 1.0, {}), ("ghost", 0.5, {}), (chunk_ids[2], 0.2, {})]
    fast = arm.collapse(ctx, scored)
    catalog = arm.catalog
    arm.catalog = SimpleNamespace(get_chunks=catalog.get_chunks, get=catalog.get)
    slow = arm.collapse(ctx, scored)
    assert [h.model_dump() for h in fast] == [h.model_dump() for h in slow] and fast


def test_selection_collapse_copies_do_not_alias_the_input():
    first = Candidate(record_id="r1", arm="lexical", score=2.0, chunk_ids=["a"], provenance=[{"chunk_id": "a"}], metadata={"k": "v"})
    second = Candidate(record_id="r1", arm="lexical", score=1.0, chunk_ids=["b"], provenance=[{"chunk_id": "b"}])
    merged = collapse_arm([first, second])
    assert merged[0].chunk_ids == ["a", "b"] and first.chunk_ids == ["a"] and len(first.provenance) == 1
    merged[0].provenance[0]["chunk_id"] = "changed"
    merged[0].metadata["k"] = "w"
    assert first.provenance[0]["chunk_id"] == "a" and first.metadata == {"k": "v"}


def test_source_slots_keep_records_the_arm_put_first():
    from su_multi_arm.selection.limits import limit

    pool = [
        Candidate(record_id="kb:1", arm="lexical", score=9.0, source="kb", leads=True),
        Candidate(record_id="case:1", arm="lexical", score=5.0, source="case"),
        Candidate(record_id="case:2", arm="lexical", score=4.0, source="case"),
        Candidate(record_id="kb:2", arm="lexical", score=3.0, source="kb"),
    ]
    slots = ["case", "kb", "case"]
    assert [c.record_id for c in limit({"lexical": pool}, 3, slots)["lexical"]] == ["kb:1", "case:1", "kb:2"]
    unmarked = [c.model_copy(update={"leads": False}) for c in pool]
    assert [c.record_id for c in limit({"lexical": unmarked}, 3, slots)["lexical"]] == ["case:1", "kb:1", "case:2"]
    merged = collapse_arm([pool[0], pool[0].model_copy(update={"leads": False, "chunk_ids": ["x"]})])
    assert merged[0].leads


@pytest.mark.parametrize("engine_name", ["numpy", "faiss"])
def test_vector_scope_is_a_row_bitset_before_the_top_k(tmp_path, engine_name):
    if engine_name == "faiss":
        pytest.importorskip("faiss")
    backend = SqliteBackend(tmp_path / engine_name, vector_engine=engine_name)
    rng = np.random.default_rng(0)
    n = 300
    documents = [Document(id=f"d{i}", metadata={"part": "even" if i % 2 == 0 else "odd"}) for i in range(n)]
    chunks = [Chunk(id=f"c{i:03d}", document_id=f"d{i}", field="body", start=0, end=1, text="x") for i in range(n)]
    backend.catalog.put_documents(GEN, documents, chunks)
    matrix = rng.normal(size=(n, 16)).astype(np.float32)
    backend.vector.upsert(GEN, [c.id for c in chunks], matrix)
    backend.vector.refresh(GEN)
    query = rng.normal(size=16).astype(np.float32)
    normed = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    exact = normed @ (query / np.linalg.norm(query))
    scope = Scope(predicates=[Predicate(field="part", op="eq", value="odd")], exclude_ids=["d1"])
    hits = backend.vector.search(GEN, query, scope, 5)
    eligible = [i for i in range(n) if i % 2 == 1 and i != 1]
    expected = sorted(eligible, key=lambda i: (-exact[i], i))[:5]
    assert [h.chunk_id for h in hits] == [f"c{i:03d}" for i in expected]
    assert all(h.document_id == "d" + str(int(h.chunk_id[1:])) for h in hits)
    assert np.allclose([h.score for h in hits], exact[expected], atol=1e-5)
    assert backend.vector.search(GEN, query, Scope(document_ids=["d4"]), 5)[0].chunk_id == "c004"
    assert len(backend.vector.search(GEN, query, None, 1000)) == n


def test_a_required_or_phrase_word_the_analyzer_drops_matches_nothing(tmp_path):
    """Release review a3 B3: under ``ascii_alnum`` a non-ASCII ``must`` word analyzes to nothing; it
    must not be dropped (that widened an exact topic count to documents without the word)."""
    texts = {"d1": "tax refund for 東京 office", "d2": "tax refund for the berlin office", "d3": "unrelated"}
    unicode = build(tmp_path, texts, name="u")
    ascii_ = build(tmp_path, texts, settings=Bm25Settings(tokenizer="ascii_alnum"), name="a")
    must = LexicalQuery(kind="boolean", text="東京 tax", must=["東京", "tax"])
    assert [h.document_id for h in unicode.lexical.search(GEN, must, None, 10).items] == ["d1"]
    assert ascii_.lexical.search(GEN, must, None, 10).items == []
    phrase = LexicalQuery(kind="phrase", text="for 東京 office")
    assert ascii_.lexical.search(GEN, phrase, None, 10).items == []
    assert ascii_.lexical.unindexable(["東京", "tax"]) == ["東京"]
    assert unicode.lexical.unindexable(["東京", "tax"]) == []
