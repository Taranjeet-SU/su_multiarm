"""MongoDB ``LexicalIndex`` facet over a per-generation text index.

Scoring: MongoDB ``$text`` returns a term-frequency ``textScore`` (title weighted 2, body 1). It is
**not** BM25 and is not comparable to FTS5 or Lucene scores; the conformance kit checks membership,
scope pushdown, determinism, phrase and boolean semantics and paging only (plan §3.5). Analysis is
the server's ``english`` text index (stemming, case and diacritic insensitive), recorded as
``analyzer_id = mongo_text_english``. Scope predicates on document fields are pushed down through a
``$lookup`` into the generation's documents collection; id allow/deny lists filter ``document_id``
directly.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Sequence

from ...core.errors import BackendError
from ...core.types import Chunk, GenerationId, LexicalQuery, Page, PageResult, Scope, ScoredChunk
from ._db import KIND, generation_name, mapped, page_window, scope_filter, text_search
from .catalog import _chunk_doc

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import MongoBackend


class MongoLexical:
    def __init__(self, backend: MongoBackend):
        self._b = backend

    def index(self, gen: GenerationId, chunks: Sequence[Chunk], fields: Sequence[str]) -> None:
        from pymongo import ReplaceOne, UpdateOne

        self._b.require_generation(gen, create=True)
        selected = [c for c in chunks if not fields or c.field in fields]
        if not selected:
            return
        docs = self._b.gen_collection(gen, "documents")
        with mapped("index"):
            wanted = sorted({c.document_id for c in selected})
            titles: dict[str, str] = {}
            for row in docs.find({"_id": {"$in": wanted}}, {"doc": 1}):
                titles[row["_id"]] = json.loads(row["doc"]).get("title") or ""
            chunk_ops, lexical_ops = [], []
            for chunk in selected:
                if chunk.document_id not in titles:
                    raise BackendError(KIND, "index", f"chunk {chunk.id} references unknown document")
                chunk_ops.append(UpdateOne({"_id": chunk.id}, {"$setOnInsert": _chunk_doc(chunk)}, upsert=True))
                lexical_ops.append(
                    ReplaceOne(
                        {"_id": chunk.id},
                        {
                            "_id": chunk.id,
                            "document_id": chunk.document_id,
                            "field": chunk.field,
                            "title": titles[chunk.document_id],
                            "text": chunk.text,
                        },
                        upsert=True,
                    )
                )
            self._b.gen_collection(gen, "chunks").bulk_write(chunk_ops, ordered=True)
            self._b.gen_collection(gen, "lexical").bulk_write(lexical_ops, ordered=True)

    def refresh(self, gen: GenerationId) -> None:
        """Writes are visible once acknowledged; refresh verifies the generation and its text index."""
        self._b.require_generation(gen)
        self._b.ensure_generation_indexes(gen)

    def count(self, gen: GenerationId) -> int:
        self._b.require_generation(gen)
        with mapped("count"):
            return int(self._b.gen_collection(gen, "lexical").count_documents({}))

    def search(
        self, gen: GenerationId, query: LexicalQuery, scope: Scope | None, k: int, page: Page | None = None
    ) -> PageResult[ScoredChunk]:
        search = text_search(query)
        offset, size = page_window(page, k)
        self._b.require_generation(gen)
        if search is None:
            return PageResult(items=[], next_cursor=None)
        id_scope = scope_filter(scope.model_copy(update={"predicates": []}) if scope else None, document_key="document_id")
        if id_scope is None:
            return PageResult(items=[], next_cursor=None)
        first: dict[str, Any] = {"$text": {"$search": search}}
        if query.fields:
            first["field"] = {"$in": list(query.fields)}
        if id_scope:
            first = {"$and": [first, id_scope]}
        pipeline: list[dict[str, Any]] = [{"$match": first}]
        if scope and scope.predicates:
            field_scope = scope_filter(Scope(predicates=scope.predicates), path="doc.fields")
            pipeline += [
                {
                    "$lookup": {
                        "from": f"{generation_name(self._b.prefix, gen.dataset, gen.generation)}_documents",
                        "localField": "document_id",
                        "foreignField": "_id",
                        "as": "doc",
                    }
                },
                {"$unwind": "$doc"},
                {"$match": field_scope},
            ]
        window = min(size, max(0, k - offset))
        pipeline += [
            {"$addFields": {"score": {"$meta": "textScore"}}},
            {"$sort": {"score": -1, "_id": 1}},
            {"$skip": offset},
            {"$limit": window + 1},
            {"$project": {"_id": 1, "document_id": 1, "score": 1}},
        ]
        with mapped("search"):
            rows = list(self._b.gen_collection(gen, "lexical").aggregate(pipeline))
        kept = rows[:window]
        items = [ScoredChunk(chunk_id=r["_id"], document_id=r["document_id"], score=float(r["score"])) for r in kept]
        more = len(rows) > len(kept) and offset + len(kept) < k
        return PageResult(items=items, next_cursor=str(offset + len(kept)) if more else None)
