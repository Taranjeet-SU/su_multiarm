"""In-memory adapter whose facets satisfy every contract in ``backends/contracts.py``.

Adapters expose one facet per capability (``adapter.catalog``, ``adapter.lexical``, ``adapter.vector``,
``adapter.records``, ``adapter.graph``, ``adapter.cache``, ``adapter.ledger``, ``adapter.session``)
because the contracts share method names (``get``, ``upsert``, ``search``, ``refresh``). This fake
exists for unit, contract and conformance tests; it is exact, unranked beyond simple overlap, and
not persistent. It enforces the invariants a real adapter must: generation scoping, cache-key
prefixes, paging, the destructive-operation guard and listable-only value enumeration.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import Counter
from typing import Any, Callable, Sequence

import numpy as np

from ..communities.base import Community
from ..core.errors import BackendError
from ..core.types import (
    CacheEntry,
    Chunk,
    Document,
    Entity,
    GenerationId,
    LedgerEntry,
    LexicalQuery,
    Membership,
    MetadataSchema,
    Page,
    PageResult,
    Path,
    PathEdge,
    Predicate,
    RawReply,
    Relation,
    Scope,
    ScopePredicates,
    ScopeResult,
    ScopeSpec,
    ScoredChunk,
    SessionRecord,
    StructuredRecord,
    TraversalSpec,
    UnsupportedConstraint,
)
from ._sessions import next_record
from ._scope import MAX_MATCHED_DEFAULT, RECORD_KEYS, check_matched, document_values, matches, record_violations, scope_result, unsupported_reason, validated_hops  # noqa: F401

KIND = "memory"
CACHE_KEY = re.compile(r"^[^/]+/[0-9a-f]{32}/.+")


def tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def _contiguous(words: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return n > 0 and any(words[i : i + n] == needle for i in range(len(words) - n + 1))


def page_slice(items: list, page: Page | None) -> PageResult:
    page = page or Page()
    offset = int(page.cursor) if page.cursor else 0
    if offset < 0:
        raise BackendError(KIND, "page", "invalid cursor")
    window = items[offset : offset + page.size]
    next_cursor = str(offset + page.size) if offset + page.size < len(items) else None
    return PageResult(items=window, next_cursor=next_cursor)


class _Generation:
    def __init__(self):
        self.documents: dict[str, Document] = {}
        self.chunks: dict[str, Chunk] = {}
        self.lexical: dict[str, list[str]] = {}
        self.lexical_pending: dict[str, list[str]] = {}
        self.vectors: dict[str, tuple[list[str], np.ndarray]] = {}
        self.vectors_pending: dict[str, tuple[list[str], np.ndarray]] = {}
        self.records: dict[str, StructuredRecord] = {}
        self.schema: MetadataSchema = MetadataSchema()
        self.entities: dict[str, Entity] = {}
        self.mentions: dict[str, list[Entity]] = {}
        self.relations: list[Relation] = []
        self.memberships: list[Membership] = []
        self.communities: dict[str, Community] = {}


class _State:
    def __init__(self, clock: Callable[[], float]):
        self.clock = clock
        self.gens: dict[str, _Generation] = {}
        self.cache: dict[str, CacheEntry] = {}
        self.ledger: dict[str, list[LedgerEntry]] = {}
        self.status: dict[str, str] = {}
        self.raw: dict[str, RawReply] = {}
        self.raw_tags: dict[str, set[str]] = {}
        self.refs: dict[str, set[str]] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self.session_lock = threading.Lock()
        self.max_matched = MAX_MATCHED_DEFAULT  # same typed cap as every records adapter
        self.protected: set[str] = set()
        self.stats: Counter[str] = Counter()

    def gen(self, gen: GenerationId, create: bool = False) -> _Generation:
        key = gen.prefix()
        if key not in self.gens:
            if not create:
                raise BackendError(KIND, "generation", f"unknown generation {key}")
            self.gens[key] = _Generation()
        return self.gens[key]

    def eligible(self, state: _Generation, scope: Scope | None, document_id: str) -> bool:
        if scope is None:
            return True
        if scope.document_ids is not None and document_id not in scope.document_ids:
            return False
        if document_id in scope.exclude_ids:
            return False
        if scope.predicates:
            document = state.documents.get(document_id)
            if document is None:
                return False
            values = document_values(document)
            return all(matches(values, p) for p in scope.predicates)
        return True


class _Facet:
    def __init__(self, state: _State):
        self._s = state


class MemoryCatalog(_Facet):
    def put_documents(self, gen: GenerationId, documents: Sequence[Document], chunks: Sequence[Chunk]) -> None:
        state = self._s.gen(gen, create=True)
        for document in documents:
            state.documents[document.id] = document
        for chunk in chunks:
            if chunk.document_id not in state.documents:
                raise BackendError(KIND, "put_documents", f"chunk {chunk.id} references unknown document")
            state.chunks[chunk.id] = chunk

    def get(self, gen: GenerationId, ids: Sequence[str]) -> list[Document]:
        state = self._s.gen(gen)
        self._s.stats["catalog.get"] += 1
        self._s.stats["catalog.get.ids"] += len(ids)
        return [state.documents[i] for i in ids if i in state.documents]

    def get_chunks(self, gen: GenerationId, chunk_ids: Sequence[str]) -> list[Chunk]:
        state = self._s.gen(gen)
        self._s.stats["catalog.get_chunks"] += 1
        return [state.chunks[i] for i in chunk_ids if i in state.chunks]

    def iter_scope(self, gen: GenerationId, scope: Scope | None, page: Page) -> PageResult[Document]:
        state = self._s.gen(gen)
        items = [d for i, d in sorted(state.documents.items()) if self._s.eligible(state, scope, i)]
        return page_slice(items, page)

    def count_documents(self, gen: GenerationId) -> int:
        return len(self._s.gen(gen).documents)

    def count_chunks(self, gen: GenerationId) -> int:
        return len(self._s.gen(gen).chunks)

    def delete_generation(self, gen: GenerationId) -> None:
        key = gen.prefix()
        if key in self._s.protected:
            raise BackendError(KIND, "delete_generation", "generation is active or pinned by a live session")
        self._s.gens.pop(key, None)
        for cache_key in [k for k in self._s.cache if k.startswith(key)]:
            del self._s.cache[cache_key]


class MemoryLexical(_Facet):
    def index(self, gen: GenerationId, chunks: Sequence[Chunk], fields: Sequence[str]) -> None:
        state = self._s.gen(gen, create=True)
        for chunk in chunks:
            if fields and chunk.field not in fields:
                continue
            state.lexical_pending[chunk.id] = tokens(chunk.text)
            state.chunks.setdefault(chunk.id, chunk)

    def refresh(self, gen: GenerationId) -> None:
        state = self._s.gen(gen)
        state.lexical.update(state.lexical_pending)
        state.lexical_pending.clear()

    def count(self, gen: GenerationId) -> int:
        return len(self._s.gen(gen).lexical)

    def search(
        self, gen: GenerationId, query: LexicalQuery, scope: Scope | None, k: int, page: Page | None = None
    ) -> PageResult[ScoredChunk]:
        state = self._s.gen(gen)
        needle = tokens(query.text)
        must = [tokens(t) for t in query.must]
        must_not = [tokens(t) for t in query.must_not]
        scored = []
        for chunk_id, words in state.lexical.items():
            chunk = state.chunks[chunk_id]
            if query.fields and chunk.field not in query.fields:
                continue
            if not self._s.eligible(state, scope, chunk.document_id):
                continue
            if query.kind == "phrase":
                if not _contiguous(words, needle):
                    continue
                score = float(len(needle))
            else:
                if query.kind == "boolean" and (
                    any(not _contiguous(words, m) for m in must) or any(_contiguous(words, m) for m in must_not)
                ):
                    continue
                present = set(words)
                overlap = sum(1 for t in needle if t in present)
                if overlap == 0:
                    continue
                score = overlap / math.sqrt(len(words) or 1)
            scored.append(ScoredChunk(chunk_id=chunk_id, document_id=chunk.document_id, score=score))
        scored.sort(key=lambda s: (-s.score, s.chunk_id))
        if k < 1:
            raise BackendError(KIND, "search", "k must be positive")
        try:
            window = page or Page(size=k)
        except ValueError as exc:
            raise BackendError(KIND, "search", f"invalid page: {exc}") from None
        return page_slice(scored[:k], window)


class MemoryVector(_Facet):
    def upsert(self, gen: GenerationId, ids: Sequence[str], vectors: np.ndarray, namespace: str = "default") -> None:
        state = self._s.gen(gen, create=True)
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(ids):
            raise BackendError(KIND, "upsert", "vectors must be a 2-D array with one row per id")
        if len(set(ids)) != len(ids):
            raise BackendError(KIND, "upsert", "duplicate ids")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms > 0)
        empty = ([], np.zeros((0, matrix.shape[1]), dtype=np.float32))
        existing_ids, existing = state.vectors_pending.get(namespace, state.vectors.get(namespace, empty))
        replaced = set(ids)
        keep = [i for i, cid in enumerate(existing_ids) if cid not in replaced]
        merged_ids = [existing_ids[i] for i in keep] + list(ids)
        merged = np.vstack([existing[keep], matrix]) if keep else matrix
        state.vectors_pending[namespace] = (merged_ids, merged)

    def refresh(self, gen: GenerationId) -> None:
        state = self._s.gen(gen)
        state.vectors.update(state.vectors_pending)
        state.vectors_pending.clear()

    def count(self, gen: GenerationId, namespace: str = "default") -> int:
        ids, _ = self._s.gen(gen).vectors.get(namespace, ([], None))
        return len(ids)

    def search(
        self, gen: GenerationId, vector: np.ndarray, scope: Scope | None, k: int, namespace: str = "default"
    ) -> list[ScoredChunk]:
        state = self._s.gen(gen)
        ids, matrix = state.vectors.get(namespace, ([], None))
        if matrix is None or not ids:
            return []
        query = np.asarray(vector, dtype=np.float32)
        if query.shape != (matrix.shape[1],):
            raise BackendError(KIND, "search", "query vector dimension mismatch")
        norm = np.linalg.norm(query)
        if norm == 0:
            return []
        scores = matrix @ (query / norm)
        hits = []
        for index in np.argsort(-scores, kind="stable"):
            chunk_id = ids[int(index)]
            chunk = state.chunks.get(chunk_id)
            if chunk is None:
                raise BackendError(KIND, "search", f"vector id {chunk_id!r} is not a catalogued chunk")
            if not self._s.eligible(state, scope, chunk.document_id):
                continue
            hits.append(ScoredChunk(chunk_id=chunk_id, document_id=chunk.document_id, score=float(scores[index])))
            if len(hits) >= k:
                break
        return hits


class MemoryRecords(_Facet):
    def upsert(self, gen: GenerationId, records: Sequence[StructuredRecord], schema: MetadataSchema) -> None:
        state = self._s.gen(gen, create=True)
        state.schema = schema
        for record in records:
            violations = record_violations(schema, record)
            if violations:
                raise BackendError(
                    KIND, "upsert", f"record {record.id}: {violations[0].field} {violations[0].category}: {violations[0].message}"
                )
            state.records[record.id] = record

    def _casefold(self, state: _Generation, field: str) -> bool:
        spec = state.schema.field(field)
        return bool(spec and spec.casefold_match)

    def _matches(self, state: _Generation, record: StructuredRecord, predicate: Predicate) -> bool:
        return matches(record.values, predicate, self._casefold(state, predicate.field), state.schema.field(predicate.field))

    def _population(self, state: _Generation, acl: ScopePredicates) -> list[StructuredRecord]:
        return [r for r in state.records.values() if all(self._matches(state, r, p) for p in acl)]

    def count(self, gen: GenerationId) -> int:
        return len(self._s.gen(gen).records)

    def execute(self, gen: GenerationId, scope: ScopeSpec, acl: ScopePredicates) -> ScopeResult:
        state = self._s.gen(gen)
        population = self._population(state, acl)
        applied, unsupported, matched = [], [], population
        for predicate in scope.predicates:
            reason = unsupported_reason(state.schema, predicate)
            if reason is not None:
                unsupported.append(UnsupportedConstraint(constraint_text=predicate.field, reason=reason))
                continue
            matched = [r for r in matched if self._matches(state, r, predicate)]
            applied.append(predicate)
        check_matched(KIND, matched, self._s.max_matched)
        return scope_result(scope, matched, len(population), state.schema, applied, unsupported)

    def exists(self, gen: GenerationId, field: str, value: Any, acl: ScopePredicates) -> bool:
        state = self._s.gen(gen)
        predicate = Predicate(field=field, op="eq", value=value)
        return any(self._matches(state, r, predicate) for r in self._population(state, acl))

    def values(self, gen: GenerationId, field: str, acl: ScopePredicates, limit: int) -> list[str]:
        state = self._s.gen(gen)
        spec = state.schema.field(field)
        if spec is None or not spec.listable:
            raise BackendError(KIND, "values", f"field {field!r} is not listable")
        seen = sorted({str(r.values[field]) for r in self._population(state, acl) if r.values.get(field) is not None})
        return seen[: max(0, limit)]


class MemoryGraph(_Facet):
    def upsert(
        self,
        gen: GenerationId,
        entities: Sequence[Entity],
        relations: Sequence[Relation],
        memberships: Sequence[Membership],
    ) -> None:
        state = self._s.gen(gen, create=True)
        for entity in entities:
            state.entities.setdefault(entity.id, entity)
            known = state.mentions.setdefault(entity.id, [])
            if entity not in known:
                known.append(entity)
        for relation in relations:
            if relation.subject_id not in state.entities or relation.object_id not in state.entities:
                raise BackendError(KIND, "upsert", "relation endpoints must be stored entities")
            state.relations.append(relation)
        state.memberships.extend(memberships)

    def traverse(self, gen: GenerationId, spec: TraversalSpec, scope: Scope | None) -> list[Path]:
        hops = validated_hops(KIND, spec.hops)
        state = self._s.gen(gen)
        edges = [
            r
            for r in state.relations
            if r.edge_kind in spec.edge_kinds
            and (not spec.predicates or r.predicate in spec.predicates)
            and self._s.eligible(state, scope, r.span.document_id)
        ]
        paths: list[Path] = []
        frontier = [Path(nodes=[seed]) for seed in spec.seed_entity_ids if seed in state.entities]
        for _ in range(hops):
            next_frontier = []
            for path in frontier:
                node = path.nodes[-1]
                for relation in edges:
                    if spec.direction in {"out", "any"} and relation.subject_id == node:
                        other = relation.object_id
                    elif spec.direction in {"in", "any"} and relation.object_id == node:
                        other = relation.subject_id
                    else:
                        continue
                    if other in path.nodes:
                        continue
                    edge = PathEdge(
                        subject_id=relation.subject_id,
                        predicate=relation.predicate,
                        object_id=relation.object_id,
                        edge_kind=relation.edge_kind,
                        support=relation.support,
                        span=relation.span,
                        provenance=relation.provenance,
                        occurrences=list(relation.occurrences),
                    )
                    extended = Path(nodes=[*path.nodes, other], edges=[*path.edges, edge])
                    next_frontier.append(extended)
                    paths.append(extended)
                    if len(paths) >= spec.limit:
                        return paths
            frontier = next_frontier
        return paths

    def memberships(self, gen: GenerationId) -> list[Membership]:
        return list(self._s.gen(gen).memberships)

    def find_entities(self, gen: GenerationId, tokens_: Sequence[str], scope: Scope | None) -> list[Entity]:
        state = self._s.gen(gen)
        wanted = set(tokens_)
        found = []
        for entity_id, mentions in sorted(state.mentions.items()):
            name_tokens = set(tokens(mentions[0].name))
            if not name_tokens or not name_tokens <= wanted:
                continue
            in_scope = next((m for m in mentions if self._s.eligible(state, scope, m.span.document_id)), None)
            if in_scope is not None:
                found.append(in_scope)
        return found

    def mentions(self, gen: GenerationId, entity_ids: Sequence[str], scope: Scope | None, limit: int) -> list[Entity]:
        state = self._s.gen(gen)
        result = []
        for entity_id in entity_ids:
            for mention in state.mentions.get(entity_id, []):
                if self._s.eligible(state, scope, mention.span.document_id):
                    result.append(mention)
                    if len(result) >= limit:
                        return result
        return result

    def put_communities(self, gen: GenerationId, communities: Sequence[Community]) -> None:
        state = self._s.gen(gen, create=True)
        for community in communities:
            state.communities[community.id] = community

    def communities(self, gen: GenerationId) -> list[Community]:
        return [c for _, c in sorted(self._s.gen(gen).communities.items())]


class MemoryCache(_Facet):
    def get(self, key: str) -> CacheEntry | None:
        entry = self._s.cache.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._s.clock():
            del self._s.cache[key]
            return None
        return entry

    def put(self, key: str, value: Any, ttl: float) -> None:
        if not CACHE_KEY.match(key):
            raise BackendError(KIND, "put", "cache keys must start with a GenerationId prefix")
        if ttl <= 0:
            raise BackendError(KIND, "put", "ttl must be positive")
        self._s.cache[key] = CacheEntry(key=key, value=value, expires_at=self._s.clock() + ttl)

    def invalidate(self, prefix: str) -> int:
        doomed = [k for k in self._s.cache if k.startswith(prefix)]
        for key in doomed:
            del self._s.cache[key]
        return len(doomed)


class MemoryLedger(_Facet):
    def append(self, job_id: str, entry: LedgerEntry) -> None:
        entries = self._s.ledger.setdefault(job_id, [])
        if entries and entry.sequence <= entries[-1].sequence:
            raise BackendError(KIND, "append", "ledger sequence must increase")
        entries.append(entry)
        self._s.status.setdefault(job_id, "pending")

    def replay(self, job_id: str) -> list[LedgerEntry]:
        return list(self._s.ledger.get(job_id, []))

    def mark(self, job_id: str, status: str) -> None:
        if job_id not in self._s.ledger:
            raise BackendError(KIND, "mark", f"unknown job {job_id}")
        self._s.status[job_id] = status

    def status(self, job_id: str) -> str | None:
        return self._s.status.get(job_id)

    def put_raw(self, key: str, raw_reply: str, valid: bool, tags: Sequence[str] = ()) -> None:
        self._s.raw[key] = RawReply(key=key, raw_reply=raw_reply, valid=valid)
        if tags:
            self.tag_raw(key, tags)

    def get_raw(self, key: str) -> RawReply | None:
        return self._s.raw.get(key)

    # retention (``extras.LedgerRetention``) ----------------------------------------------------
    def tag_raw(self, key: str, tags: Sequence[str]) -> None:
        if key not in self._s.raw:
            raise BackendError(KIND, "tag_raw", f"unknown raw entry {key}")
        self._s.raw_tags.setdefault(key, set()).update(tags)

    def raw_tags(self, key: str) -> list[str]:
        return sorted(self._s.raw_tags.get(key, ()))

    def tagged(self, tag: str) -> list[str]:
        return sorted(k for k, tags in self._s.raw_tags.items() if tag in tags)

    def add_refs(self, generation: str, keys: Sequence[str]) -> None:
        self._s.refs.setdefault(generation, set()).update(keys)

    def refs(self, generation: str) -> list[str]:
        return sorted(self._s.refs.get(generation, ()))

    def referencing_generations(self) -> list[str]:
        return sorted(g for g, keys in self._s.refs.items() if keys)

    def purge(self, spec: Any) -> dict[str, int]:
        dropped = sum(len(self._s.refs.pop(g, ())) for g in spec.drop_refs_of)
        doomed = {k for tag in spec.tags for k in self.tagged(tag)}
        tagged = len(doomed)
        referenced = set().union(*self._s.refs.values()) if self._s.refs else set()
        unreferenced = {k for k in self._s.raw if k not in doomed and k not in referenced and not self._s.raw_tags.get(k) and any(k.startswith(p) for p in spec.unreferenced_prefixes)}
        for key in doomed | unreferenced:
            self._s.raw.pop(key, None)
            self._s.raw_tags.pop(key, None)
        return {"refs_dropped": dropped, "tagged": tagged, "unreferenced": len(unreferenced)}

    def drop_job(self, job_id: str) -> int:
        entries = self._s.ledger.pop(job_id, [])
        self._s.status.pop(job_id, None)
        return len(entries)

    def jobs(self) -> list[str]:
        return sorted(self._s.ledger)

    def raw_entries(self, prefix: str, after: str | None, limit: int) -> list[RawReply]:
        keys = sorted(k for k in self._s.raw if k.startswith(prefix) and (after is None or k > after))
        return [self._s.raw[k] for k in keys[:limit]]


class MemorySessions(_Facet):
    def create(self, record: SessionRecord) -> None:
        if record.id in self._s.sessions:
            raise BackendError(KIND, "create", f"session {record.id} already exists")
        self._s.sessions[record.id] = record

    def get(self, session_id: str) -> SessionRecord | None:
        return self._s.sessions.get(session_id)

    def touch(self, session_id: str, now: float, record: SessionRecord | None = None, expected_version: int | None = None) -> None:
        with self._s.session_lock:
            current = self._s.sessions.get(session_id)
            if current is None:
                raise BackendError(KIND, "touch", f"unknown session {session_id}")
            self._s.sessions[session_id] = next_record(session_id, current, now, record, expected_version)

    def expire(self, now: float, ttl_seconds: float) -> int:
        doomed = [k for k, s in self._s.sessions.items() if s.last_seen_at + ttl_seconds <= now]
        for key in doomed:
            del self._s.sessions[key]
        return len(doomed)


class MemoryBackend:
    """One shared state, eight facets. ``protect(gen)`` marks a generation as CURRENT/pinned."""

    kind = KIND

    def __init__(self, clock: Callable[[], float] = time.time):
        self._state = _State(clock)
        self.catalog = MemoryCatalog(self._state)
        self.lexical = MemoryLexical(self._state)
        self.vector = MemoryVector(self._state)
        self.records = MemoryRecords(self._state)
        self.graph = MemoryGraph(self._state)
        self.cache = MemoryCache(self._state)
        self.ledger = MemoryLedger(self._state)
        self.session = MemorySessions(self._state)

    @property
    def max_matched(self) -> int:
        return self._state.max_matched

    @max_matched.setter
    def max_matched(self, value: int) -> None:
        self._state.max_matched = value

    @property
    def stats(self) -> Counter[str]:
        return self._state.stats

    def probe(self) -> dict[str, Any]:
        """``doctor --probe``: the in-process fake is always reachable; reports its contents."""
        return {"in_process": True, "generations": len(self._state.gens), "sessions": len(self._state.sessions)}

    def protect(self, gen: GenerationId) -> None:
        self._state.protected.add(gen.prefix())

    def unprotect(self, gen: GenerationId) -> None:
        self._state.protected.discard(gen.prefix())

    def generations(self) -> list[str]:
        return sorted(self._state.gens)

    def generation_identity(self, gen: GenerationId) -> dict[str, Any]:
        return {"key": gen.prefix()}

    def generation_registry(self):
        """Pointer/manifest registry for this backend (non-contract; see ``ingestion/release.py``)."""
        from ..ingestion.release import MemoryGenerationRegistry

        if not hasattr(self, "_registry"):
            self._registry = MemoryGenerationRegistry(self)
        return self._registry
