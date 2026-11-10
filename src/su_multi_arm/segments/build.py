"""``su-multi-arm segments build``: read segment rows, anchor them in the active generation, embed
them with the configured embedder and seal the index. The ``segment`` arm itself is not needed (or
built) for this: it would refuse to start without the index being built here."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.types import Page, Scope
from . import build_segment_index, read_segments


def _without_segment_arm(config: Any) -> Any:
    copy = config.model_copy(deep=True)
    copy.pipeline.arms.segment = None
    for overlay in getattr(copy, "pipelines", {}).values():
        arms = getattr(overlay, "arms", None)
        if arms is not None and getattr(arms, "segment", None) is not None:
            arms.segment = None
    return copy


def build_from_config(config: Any, input_path: Path, out_dir: Path, *, anchor: str = "required", batch_size: int = 64, engine: Any = None) -> dict[str, Any]:
    """Build an index for ``config``'s active generation. ``engine`` (tests) skips construction."""
    if engine is None:
        from ..engine import Engine

        engine = Engine(_without_segment_arm(config))
    rows = read_segments(Path(input_path))
    gen = engine.write_path.current()[0]
    catalog = engine.backends[engine.config.storage.catalog].catalog
    parent_ids = sorted({r.parent_id for r in rows})
    parents: dict[str, Any] = {}
    for start in range(0, len(parent_ids), 500):
        batch = parent_ids[start : start + 500]
        cursor = None
        while True:
            page = catalog.iter_scope(gen, Scope(document_ids=batch), Page(cursor=cursor, size=500))
            parents.update((d.id, d) for d in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
    embedder = getattr(engine, "embedder", None)
    if embedder is None:
        raise ValueError("segments build needs models.roles.embedder")
    return build_segment_index(rows, Path(out_dir), embedder=embedder, parents=parents, dataset=gen.dataset, generation=gen.generation, anchor=anchor, batch_size=batch_size)
