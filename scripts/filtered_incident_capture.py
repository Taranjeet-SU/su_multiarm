"""Exact filtered seed scroll; injected compiler/transport/privacy, no unfiltered fallback.

This is a separate incident-seed contract. It never imports records, claims whole
source coverage, or claims neighboring context has been captured. The caller must
seal its output only after this function returns successfully.
"""

from __future__ import annotations

import copy
from pathlib import Path
import sqlite3
import tempfile

from grouped_log_capture import CaptureError, SOURCE_FIELDS, exact_total

CONTRACT = "elk_incidents_v1"


def _contains_match_all(value):
    if isinstance(value, dict):
        return "match_all" in value or any(_contains_match_all(v) for v in value.values())
    return isinstance(value, list) and any(_contains_match_all(v) for v in value)


def capture_filtered_seed(es, scope, compiled, validate_initial, emit, sanitize, *, work_dir):
    """Capture all unique matches from one filtered scroll snapshot, with page size1000.

    ``compiled`` and ``validate_initial`` come from the same policy compiler. The
    initial global aggregation supplies the independent window-scoped population;
    exact hits.total is the selected subset. Counts are never substituted.
    """
    request = copy.deepcopy(compiled["request"])
    query = request.get("query", {})
    root = query.get("bool", {})
    window = compiled.get("window")
    population = {"global": {}}
    if window is not None:
        if not isinstance(window, dict) or set(window) != {"start", "end"}:
            raise CaptureError("filtered capture requires explicit window identity")
        bounded = {"range": {"@timestamp": {"gte": window["start"], "lt": window["end"]}}}
        if set(root) != {"must", "filter"} or root["filter"] != [bounded] or len(root["must"]) != 1:
            raise CaptureError("filtered query window differs from recorded window")
        root = root["must"][0].get("bool", {})
        population["aggs"] = {"window": {"filter": bounded}}
    if not root.get("should") or root.get("minimum_should_match") != 1 or _contains_match_all(query):
        raise CaptureError("filtered capture requires explicit positive seed branches")
    if request.get("aggs", {}).get("source_scope") != population:
        raise CaptureError("filtered capture requires same-window source population")
    request.update(
        size=1000, sort=["_doc"], track_total_hits=True, timeout="50s", _source={"includes": SOURCE_FIELDS}
    )
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    sid, fetched, requests, early_pages = None, 0, 0, 0
    with tempfile.TemporaryDirectory(prefix="incident-seeds-", dir=work_dir) as scratch:
        seen = sqlite3.connect(str(Path(scratch) / "seen.sqlite"))
        seen.execute("CREATE TABLE ids(identity TEXT PRIMARY KEY) WITHOUT ROWID")
        try:
            response = es.request(
                "POST",
                f"/{scope['index']}/_search?scroll=2m&allow_partial_search_results=false",
                request,
                note="incident_seed_snapshot",
            )
            requests += 1
            sid = response.get("_scroll_id")
            report = validate_initial(response, compiled, scope)
            selected = report["selected_total"]
            if exact_total(response) != selected:
                raise CaptureError("filtered compiler and scroll count differ")
            while True:
                checked = response
                if requests > 1 and response.get("terminated_early"):
                    early_pages += 1
                    checked = {**response, "terminated_early": False}
                if exact_total(checked) != selected:
                    raise CaptureError("filtered scroll selected total changed")
                hits = response.get("hits", {}).get("hits")
                if not isinstance(hits, list):
                    raise CaptureError("filtered source omitted hits")
                for hit in hits:
                    identity = hit.get("_id")
                    if hit.get("_index") != scope["index"] or not isinstance(identity, str) or not identity:
                        raise CaptureError("filtered source identity is invalid")
                    try:
                        seen.execute("INSERT INTO ids VALUES(?)", (identity,))
                    except sqlite3.IntegrityError:
                        raise CaptureError("filtered source repeated identity") from None
                    record = sanitize(hit)
                    emit({"contract": CONTRACT, "kind": "incident_seed", "record": record})
                    fetched += 1
                    if fetched > selected:
                        raise CaptureError("filtered scroll exceeds selected total")
                if fetched == selected:
                    break
                if not hits or not sid:
                    raise CaptureError("filtered scroll ended before selected total")
                response = es.scroll(sid, "2m")
                requests += 1
                sid = response.get("_scroll_id", sid)
            unique = seen.execute("SELECT COUNT(*) FROM ids").fetchone()[0]
            if unique != selected or fetched != selected:
                raise CaptureError("filtered IDs and selected total do not reconcile")
            return {
                **report,
                "contract": CONTRACT,
                "phase": "seed_capture",
                "records_fetched": fetched,
                "unique_source_ids": unique,
                "count_reconciled": True,
                "complete_for_selected_seeds": True,
                "complete_for_scope": False,
                "full_capture": False,
                "grouped_capture": False,
                "context_status": "not_fetched",
                "context_pending": True,
                "snapshot": {"protocol": "scroll", "snapshot_consistent": True, "requests": requests},
                "scroll_page_size": 1000,
                "scroll_terminated_early_pages": early_pages,
            }
        finally:
            try:
                if sid:
                    es.clear_scroll(sid)
            finally:
                seen.close()
