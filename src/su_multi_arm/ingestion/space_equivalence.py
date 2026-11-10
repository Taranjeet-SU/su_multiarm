"""Embedding-space equivalence of an adopted member (docs/plans/unified_merge.md §6.2).

A unified configuration may declare its own ``collection.embedding_space.id`` (a stable name for the
one space every member the dense arm reads lives in). An adopted store keeps the space id its
original configuration derived (the model name), so its recorded ``EmbeddingSpaceRef`` differs from
the collection's by the id alone and compose refuses it (staging F5). The explicit mapping
``collection.members.<id>.adopt.embedding_space: <the store's space id>`` declares the two
equivalent; compose accepts the member only when

* the two refs are equal except ``id`` and ``epoch`` (model identity, revision, dimensions,
  normalization and input template equal: ``differences``), and
* a **verify sample** was re-embedded online and compared with the store's own vectors — at least
  ``embedding_space.equivalence.sample`` chunks (default 200; every chunk when the store holds
  fewer), stratified by document kind, mean cosine ≥ ``mean_cosine`` (0.995) and minimum ≥
  ``min_cosine`` (0.98) — and the passing result is recorded in the member registry
  (``space_equivalence.json`` next to the member's ``adoption.json``; the adopted store is only read).

``verify`` runs the check (embedder calls; the CLI ``collection verify-space --member``);
``accepted`` is what compose reads (no embedder call, no store read). A failed check is recorded too
(and refuses compose); a changed collection space, store generation or threshold invalidates the
record (refused until verified again). Nothing is retried or relaxed.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

RECORD = "space_equivalence.json"
TEMPLATE = "{title}\n{field}: {text}"  # ``ingestion.write.EMBEDDING_INPUT`` (the engine template)


class SpaceEquivalenceError(ValueError):
    """The equivalence cannot be checked or did not pass."""


def differences(member: Any, collection: Any) -> list[str]:
    """Fields other than ``id``/``epoch`` in which two ``EmbeddingSpaceRef`` differ."""
    return [name for name in ("model", "revision", "dimensions", "normalization", "template") if getattr(member, name) != getattr(collection, name)]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _expected(config: Any, ref: Any) -> dict[str, Any]:
    """What a record must name to be valid for ``ref`` under ``config``."""
    from .recipes import embedding_space_ref

    space = embedding_space_ref(config)
    check = config.collection.embedding_space.equivalence
    return {
        "unit": ref.unit_id,
        "partition_id": ref.partition_id,
        "member_space": ref.recipe.embedding_space.model_dump(mode="json") if ref.recipe.embedding_space is not None else None,
        "collection_space": space.model_dump(mode="json"),
        "thresholds": {"sample": check.sample, "mean_cosine": check.mean_cosine, "min_cosine": check.min_cosine},
    }


def mapping_problem(config: Any, ref: Any) -> str | None:
    """Why the declared mapping does not cover ``ref`` (``None``: it does)."""
    from .recipes import embedding_space_ref

    declared = config.collection.members.get(ref.member)
    adopt = getattr(declared, "adopt", None)
    space = ref.recipe.embedding_space
    if ref.tier != "adopted" or adopt is None or adopt.embedding_space is None or space is None:
        return "no adopt.embedding_space mapping is declared for this member"
    if adopt.embedding_space != space.id:
        return f"collection.members.{ref.member}.adopt.embedding_space is {adopt.embedding_space!r}, the adopted store's space is {space.id!r}"
    diff = differences(space, embedding_space_ref(config))
    if diff:
        return f"the adopted store's space differs from the collection's in {diff} (only the id may differ; re-embed the member)"
    return None


def accepted(config: Any, registry: Any, ref: Any) -> str | None:
    """``None`` when compose may accept ``ref`` under the declared mapping; else the reason."""
    problem = mapping_problem(config, ref)
    if problem is not None:
        return problem
    path = registry.unit_dir(ref) / RECORD
    if not path.is_file():
        return f"the equivalence of member {ref.member!r}'s embedding space is not verified (run: collection verify-space --member {ref.member})"
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return f"the equivalence record of member {ref.member!r} cannot be read: {exc}"
    if record.get("expected") != _digest(_expected(config, ref)):
        return f"the equivalence record of member {ref.member!r} was made for another space, store generation or threshold (verify again)"
    if not record.get("passed"):
        return f"the embedding-space equivalence check of member {ref.member!r} failed: {record.get('reason')}"
    return None


def _sample(db: Any, size: int, seed: int) -> list[tuple[str, str, str, str]]:
    """``(chunk id, title, field, text)`` of up to ``size`` chunks stratified by document kind
    (round robin over kinds in name order; seeded order inside a kind)."""
    by_kind: dict[str, list[tuple[str, str, str, str]]] = {}
    rows = db.execute("SELECT c.id, c.document_id, c.field, c.text, d.data FROM chunks c JOIN documents d ON d.id = c.document_id ORDER BY c.id").fetchall()
    for chunk_id, _doc, field, text, data in rows:
        doc = json.loads(data)
        by_kind.setdefault(str(doc.get("kind") or ""), []).append((chunk_id, str(doc.get("title") or ""), field, text or ""))
    rng = random.Random(seed)
    for items in by_kind.values():
        rng.shuffle(items)
    out: list[tuple[str, str, str, str]] = []
    kinds = sorted(by_kind)
    while len(out) < size and any(by_kind[k] for k in kinds):
        for kind in kinds:
            if by_kind[kind] and len(out) < size:
                out.append(by_kind[kind].pop())
    return out


def verify(config: Any, registry: Any, member: str, encode: Callable[[list[str]], Any], *, seed: int = 0, now: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """Re-embed a stratified sample of the adopted member's chunks with ``encode`` (the online
    embedder), compare with the store's vectors and record the result (module doc). Returns the
    record; raises ``SpaceEquivalenceError`` when the mapping does not apply or nothing can be
    compared. A check below the thresholds is recorded as failed (compose then refuses)."""
    from ..backends.sqlite.members import adopted_backend
    from .release import FileGenerationRegistry, generation_id

    refs = [r for r in registry.live_members() if r.member == member and r.tier == "adopted"]
    if len(refs) != 1:
        raise SpaceEquivalenceError(f"member {member!r} has {len(refs)} live adopted units (adopt it first)")
    ref = refs[0]
    problem = mapping_problem(config, ref)
    if problem is not None:
        raise SpaceEquivalenceError(problem)
    check = config.collection.embedding_space.equivalence
    store = registry.store_dir(ref)
    manifest = FileGenerationRegistry(store).current()
    if manifest is None:
        raise SpaceEquivalenceError(f"member {member!r}: the adopted store has no active generation")
    import time

    backend = adopted_backend(store, ref, time.time)
    gen = generation_id(manifest)
    with backend.catalog_db(gen) as db:
        picked = _sample(db, check.sample, seed)
        total = int(db.execute("SELECT count(*) FROM chunks").fetchone()[0])
    stored = backend.vector.rows(gen, [p[0] for p in picked])
    compared = [p for p in picked if p[0] in stored]
    if not compared:
        raise SpaceEquivalenceError(f"member {member!r}: no stored vector to compare (the adopted store has no dense vectors)")
    fresh = np.asarray(encode([TEMPLATE.format(title=t, field=f, text=x) for _c, t, f, x in compared]), dtype=np.float32)
    rows = np.vstack([stored[c] for c, _t, _f, _x in compared])
    fresh /= np.maximum(np.linalg.norm(fresh, axis=1, keepdims=True), 1e-12)
    rows /= np.maximum(np.linalg.norm(rows, axis=1, keepdims=True), 1e-12)
    cosines = (fresh * rows).sum(axis=1)
    mean, low = float(cosines.mean()), float(cosines.min())
    enough = len(compared) >= min(check.sample, total)
    passed = bool(enough and mean >= check.mean_cosine and low >= check.min_cosine)
    reason = None
    if not enough:
        reason = f"only {len(compared)} of the required {min(check.sample, total)} sampled chunks have a stored vector"
    elif not passed:
        reason = f"mean cosine {mean:.6f} (required {check.mean_cosine}) / min cosine {low:.6f} (required {check.min_cosine})"
    expected = _expected(config, ref)
    record = {
        "format": 1,
        "expected": _digest(expected),
        **expected,
        "checked": len(compared),
        "store_chunks": total,
        "mean_cosine": round(mean, 6),
        "min_cosine": round(low, 6),
        "below_min": int((cosines < check.min_cosine).sum()),
        "seed": seed,
        "passed": passed,
        "reason": reason,
        "checked_at": (now() if now is not None else datetime.now(timezone.utc)).isoformat(),
    }
    folder = registry.unit_dir(ref)
    folder.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".tmp_")
    try:
        with os.fdopen(handle, "w") as out:
            json.dump(record, out, indent=2, sort_keys=True)
        os.replace(temp, folder / RECORD)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return record
