"""QA-07: lexical query normalisation matches the index analyzer.

The lexical arm and the sqlite FTS5 adapter no longer ``str.casefold`` the query: FTS5 re-tokenises
every quoted term with the same ``unicode61`` tokenizer the text was indexed with, so case folding is
exactly the index's (``ß`` and ligatures are kept, as ``unicode61`` keeps them). Synthetic text only.
"""

from __future__ import annotations

from su_multi_arm.arms.lexical import query_terms
from su_multi_arm.backends.sqlite import SqliteBackend
from su_multi_arm.backends.sqlite.search import fts_expression
from su_multi_arm.core.types import Chunk, LexicalQuery
from su_multi_arm.testing import backend_conformance as kit

TEXT = "Die Straße ist gesperrt. Das ﬁle liegt im Œuvre-Ordner. ΣΟΦΟΣ"


def test_query_terms_keep_the_spelling_and_drop_case_repeats():
    assert query_terms("Straße straße STRASSE ﬁle File") == ["Straße", "STRASSE", "ﬁle", "File"]
    assert query_terms("") == []


def test_fts_expression_does_not_fold_beyond_unicode61():
    assert fts_expression(LexicalQuery(text="Straße ﬁle")) == '("Straße" OR "ﬁle")'
    assert fts_expression(LexicalQuery(text="Straße straße")) == '("Straße")'  # repeats dropped by lower()
    assert fts_expression(LexicalQuery(kind="phrase", text="Die Straße")) == '"Die Straße"'


def _hits(backend, text):
    return [h.document_id for h in backend.lexical.search(kit.GEN, LexicalQuery(text=text), None, 10).items]


def test_sqlite_matches_its_own_indexed_words(tmp_path):
    backend = SqliteBackend(tmp_path / "s")
    kit.seed(backend, kit.GEN)
    doc = kit.document(9)
    chunk = Chunk(id="c9", document_id=doc.id, field="body", start=0, end=len(TEXT), text=TEXT)
    backend.catalog.put_documents(kit.GEN, [doc], [chunk])
    backend.lexical.index(kit.GEN, [chunk], ["body"])
    for word in ("Straße", "straße", "ﬁle", "gesperrt", "GESPERRT", "Œuvre", "œuvre", "σοφος", "ΣΟΦΟΣ"):
        assert doc.id in _hits(backend, word), word
    # unicode61 expands neither ß nor ligatures: these are different words (documented behaviour).
    assert doc.id not in _hits(backend, "strasse") and doc.id not in _hits(backend, "file")
