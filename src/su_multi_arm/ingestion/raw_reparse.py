"""The only gateway to the raw-copy store (``logs.partitions.raw``; A2 §11.R4 n-r3-1).

``raw_store.RawStore`` is the file layout (write, list, expire); this module is the only importer of
it. Entry points:

- ``open_raw``: the store of a configured source, or ``None`` when raw copies are off (the puller,
  retention, the CLI audit);
- ``write_unit``: the puller's unredacted copy, written before redaction;
- ``reparse_from_raw`` / ``rederive``: ``logs reparse --from-raw`` and the ``rederive`` step of
  ``run-daily`` (a redaction change re-derives only days the raw tier still holds);
- ``audit_root``: ``privacy scan --raw``.

Raw content is read only by ``read_unit``/``read_day`` here. An import-graph test asserts that
``schedule.py``, ``learning/`` and every other module reach the raw store only through this module.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Iterator

from .raw_store import READ_PURPOSES, RawAccessRefused, RawStore, RawUnavailable


def open_raw(config: Any) -> RawStore | None:
    """The raw store of ``config.logs`` (``None`` when ``logs.partitions.raw.enabled`` is false)."""
    raw = config.logs.partitions.raw
    return RawStore(raw.path, config.logs.source, raw.compression) if raw.enabled else None


def write_unit(store: RawStore, key: str, unit: Any, count: Any, sample: Any, capped: bool, restarts: int, docs: list[Any]) -> None:
    """The unredacted copy of one pulled unit (mode 0600), with what reparse needs to rebuild it."""
    store.put_unit(
        key,
        unit.id,
        {
            "unit": unit.model_dump(mode="json"),
            "count": count.model_dump(mode="json"),
            "sample": sample.model_dump(mode="json"),
            "capped": capped,
            "restarts": restarts,
            "docs": [d.model_dump(mode="json") for d in docs],
        },
    )


def read_unit(store: RawStore, key: str, unit_id: str, *, purpose: str) -> dict[str, Any]:
    if purpose not in READ_PURPOSES:
        raise RawAccessRefused(f"raw copies are readable only for {sorted(READ_PURPOSES)}, not {purpose!r}")
    path = store.unit_path(key, unit_id)
    if not path.exists():
        raise RawUnavailable(f"no raw copy of unit {unit_id} of {store.source}/{key}; use logs settle (repull) to pull it again")
    data = path.read_bytes()
    return json.loads(gzip.decompress(data) if store.compression == "gzip" else data)


def read_day(store: RawStore, key: str, unit_ids: list[str], *, purpose: str) -> Iterator[dict[str, Any]]:
    missing = sorted(set(unit_ids) - set(store.unit_ids(key)))
    if missing:
        raise RawUnavailable(f"the raw copy of {store.source}/{key} lacks {len(missing)} unit(s); use logs settle (repull) to pull them again")
    for unit_id in sorted(unit_ids):
        yield read_unit(store, key, unit_id, purpose=purpose)


def audit_root(store: RawStore, *, purpose: str) -> Path:
    """The raw root for ``privacy scan --raw`` (purpose ``audit`` only)."""
    if purpose != "audit":
        raise RawAccessRefused(f"the raw root is handed out only for the audit, not {purpose!r}")
    return store.root


def reparse_from_raw(pipe: Any, key: str, sealed: Any, library: Any) -> dict[str, Any]:
    """Redaction, multiline assembly and parse again from the raw copies of day ``key`` into a new
    sealed version (the caller holds the schedule lock)."""
    from ..sources.contracts import SamplePlan, SourceDoc, SourceUnit, UnitCount

    if pipe.raw is None:
        raise RawUnavailable(f"raw copies are not enabled (logs.partitions.raw); reparse {key} from its redacted records, or repull it with logs settle")
    pipe.require_redaction()
    payloads = list(read_day(pipe.raw, key, sealed.units, purpose="reparse"))
    pipe.store.restart(key, library.provenance())
    for payload in payloads:
        unit = SourceUnit.model_validate(payload["unit"])
        docs = [SourceDoc.model_validate(d) for d in payload["docs"]]
        artifacts = pipe._artifacts(
            unit, key, UnitCount.model_validate(payload["count"]), SamplePlan.model_validate(payload["sample"]), docs, bool(payload["capped"]), int(payload["restarts"]), library
        )
        pipe.store.put_unit(key, unit.id, artifacts)
    manifest = pipe.store.seal(key, pipe.parse(list(pipe.store.units(key)), library), provenance=library.provenance(), redaction=pipe.mapping.redaction_report())
    return {"status": "reparsed", "version": manifest.version, "from_raw": True, "units": len(payloads)}


def rederive(pipe: Any, library: Any) -> dict[str, Any]:
    """Re-derive the sealed days whose redaction differs from the current one, **only** where the raw
    tier still holds the whole day. Days outside the raw tier keep their old redaction and are listed
    (``stale_redaction``): never re-pulled silently."""
    current = pipe.mapping.redaction
    stale = [m for m in pipe.store.list() if m.status == "sealed" and (m.redaction or {}).get("mode") != current]
    if not stale:
        return {"status": "current", "rederived": [], "stale_redaction": []}
    raw_days = set(pipe.raw.keys()) if pipe.raw is not None else set()
    done, left = [], []
    for manifest in sorted(stale, key=lambda m: m.key):
        if manifest.key in raw_days and set(manifest.units) <= set(pipe.raw.unit_ids(manifest.key)):
            reparse_from_raw(pipe, manifest.key, manifest, library)
            done.append(manifest.key)
        else:
            left.append(manifest.key)
    return {"status": "rederived" if done else "stale", "rederived": done, "stale_redaction": left}
