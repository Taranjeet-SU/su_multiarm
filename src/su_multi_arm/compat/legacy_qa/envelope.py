"""``AnswerResult`` → legacy QA envelope (question, answer with ``[n]`` markers, numbered citations,
plan, timings and per-arm panels), plus the ``compat`` block that names every legacy field the package
cannot fill.

Citation numbering. Evidence is numbered 1..N in the order the engine packed it (the order of the
stream's ``evidence`` event), so the numbers in a streamed ``evidence`` event, the final ``citations``
and the ``[n]`` markers in the answer text always agree. The generator cites evidence ids as ``[id]``
(or ``[id1, id2]``); each marker whose ids are all packed evidence becomes ``[n]`` (``[n][m]``).
Anything else in square brackets (Markdown links, text) is left untouched; cited ids that are not
packed evidence cannot occur (the answer contract rejects unknown evidence).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from ... import __version__
from ...models import AnswerResult, Hit
from .profile import DatasetProfile

ARM_MODES = {
    "dense": "dense",
    "lexical": "lexical",
    "structured": "sql",
    "typed_query": "sql",
    "sql_surface": "sql",
    "graph": "graph",
    "community": "community",
    "segment": "cases",
    "exact_id": "exact_id",
    "cache": "cache",
}
SNIPPET_CHARS = 600
PANEL_TEXT_CHARS = 500
_MARKER = re.compile(r"\[([^\[\]\n]{1,800})\](?!\()")

# Legacy fields with no package equivalent, always reported (the reason is the contract text).
ALWAYS_UNSUPPORTED = {
    "synthetic": "no synthetic answer layer in the package",
    "synth_evidence": "no synthetic answer layer in the package",
    "kg2": "no KG v2 diagnostic trace in the package",
    "delivered_evidence": "the packed evidence text is not exposed; see compat.delivered_evidence_chars",
}


def legacy_modes(arms: list[str]) -> list[str]:
    return list(dict.fromkeys(ARM_MODES.get(a, a) for a in arms))


def source_link(profile: DatasetProfile, record_id: str, channel: str | None = None) -> str | None:
    """Relative record-view link, when the source view is enabled and the id is viewable. On a
    ``source.ui_link`` channel the link carries the UI's dataset token and the id alias when a rule
    applies (``GET /source`` resolves it back); otherwise the dataset id and the package id."""
    view = profile.source
    if not view.enabled or not re.fullmatch(view.id_pattern, record_id):
        return None
    link = view.ui_link
    if link is not None and channel in link.channels:
        alias = link.alias(record_id)
        if alias is not None:
            return f"{view.path}?{urlencode({'dataset': link.dataset, 'id': alias})}"
    return f"{view.path}?{urlencode({'dataset': profile.id, 'id': record_id})}"


def citation_url(profile: DatasetProfile, record_id: str, external: str | None, channel: str | None = None) -> str:
    """The URL a citation shows: the record's absolute URL and/or the relative source view, by the
    profile's ``citation_url`` policy; ``""`` when neither exists (the legacy UI then shows no link)."""
    relative = source_link(profile, record_id, channel)
    if profile.citation_url == "external_only":
        return external or ""
    if profile.citation_url == "source_first":
        return relative or external or ""
    return external or relative or ""


def content_source(profile: DatasetProfile, source: str | None) -> str:
    return profile.source_label(source)


def aggregate_title(aggregate: dict[str, Any]) -> str:
    rendering = str(aggregate.get("rendering") or "").strip().splitlines()
    head = rendering[0] if rendering else str(aggregate.get("operation") or "aggregate")
    return "Structured result: " + (head[:160] + ("…" if len(head) > 160 else ""))


def number_evidence(profile: DatasetProfile, items: list[dict[str, Any]], result: AnswerResult | None, channel: str | None = None) -> list[dict[str, Any]]:
    """Numbered citation objects for the packed evidence, in packing order."""
    hits: dict[str, Hit] = {}
    aggregates: dict[str, dict[str, Any]] = {}
    if result is not None:
        hits = {h.document_id: h for h in result.retrieval.hits}
        aggregates = {a["id"]: a for a in result.retrieval.aggregates if "id" in a}
    out = []
    for n, item in enumerate(items, start=1):
        rid = str(item.get("id"))
        hit = hits.get(rid)
        if rid in aggregates or item.get("source") in {"aggregate", "structured"}:
            aggregate = aggregates.get(rid, {})
            out.append({"id": n, "title": aggregate_title(aggregate) if aggregate else (item.get("title") or rid), "url": "", "record_id": rid, "content_source": "Structured result", "record_type": "aggregate", "snippet": str(aggregate.get("rendering") or "")[:SNIPPET_CHARS] or None})
            continue
        source = item.get("source") or (hit.source if hit else None)
        external = item.get("url") or (hit.url if hit else None)
        citation = {
            "id": n,
            "title": item.get("title") or (hit.title if hit else "") or rid,
            "url": citation_url(profile, rid, external, channel),
            "record_id": rid,
            "canonical_record_id": rid,
            "content_source": content_source(profile, source),
            "record_type": source,
            "snippet": (hit.text[:SNIPPET_CHARS] if hit else None),
        }
        if hit and hit.chunk_ids:
            citation["chunk_id"] = hit.chunk_ids[0]
        out.append(citation)
    return out


def renumber(text: str, numbers: dict[str, int]) -> str:
    """``[id]`` / ``[id1, id2]`` markers of packed evidence → ``[n]`` / ``[n][m]``."""

    def swap(match: re.Match) -> str:
        parts = [p.strip() for p in re.split(r"[,;]", match.group(1))]
        if parts and all(p in numbers for p in parts):
            return "".join(f"[{numbers[p]}]" for p in dict.fromkeys(parts))
        return match.group(0)

    return _MARKER.sub(swap, text)


def _ms(seconds: Any) -> int:
    try:
        return int(round(float(seconds) * 1000))
    except (TypeError, ValueError):
        return 0


def rerank_seconds(selection: dict[str, Any] | None) -> float | None:
    """Measured reranking time (``selection.rerank_detail.seconds``), ``None`` when no rerank ran."""
    detail = (selection or {}).get("rerank_detail") or {}
    seconds = detail.get("seconds")
    return float(seconds) if isinstance(seconds, (int, float)) else None


def timings(result: AnswerResult, wall_seconds: float, selection: dict[str, Any] | None = None) -> dict[str, Any]:
    trace = result.trace or {}
    decision = trace.get("decision") or {}
    calls = trace.get("model_calls") or []
    answer_s = sum(float(c.get("seconds") or 0) for c in calls if c.get("stage") == "generator")
    planner_s = float(decision.get("seconds") or 0)
    retrieval_s = max(0.0, float(result.retrieval.trace.get("seconds") or 0) - planner_s)
    stages: dict[str, int] = {}
    for call in calls:
        key = f"llm:{call.get('stage')}"
        stages[key] = stages.get(key, 0) + _ms(call.get("seconds"))
    for step in result.retrieval.trace.get("steps") or []:
        key = f"arm:{step.get('arm')}"
        stages[key] = stages.get(key, 0) + _ms(step.get("seconds"))
    return {
        "total_ms": _ms(wall_seconds),
        "planner_ms": _ms(planner_s),
        "retrieval_ms": _ms(retrieval_s),
        "rerank_ms": _ms(rerank_seconds(selection) or 0),
        "answer_ms": _ms(answer_s),
        "stages": stages,
    }


def plan_block(result: AnswerResult, planner_model: str | None) -> dict[str, Any]:
    decision = (result.trace or {}).get("decision") or {}
    arms = [s.get("arm") for s in result.retrieval.trace.get("steps") or [] if s.get("arm")]
    if result.retrieval.aggregates and "structured" not in arms:
        arms.append("structured")
    kind = decision.get("kind")
    reason = decision.get("note") or f"{decision.get('outcome', 'decided')} ({decision.get('policy_applied', kind)})"
    return {
        "selected_modes": legacy_modes(arms),
        "reason": reason,
        "planner": planner_model if kind in {"llm", "cascade"} and planner_model else str(kind or "rules"),
        "intent": ",".join(decision.get("intents") or []) or "retrieval",
        "outcome": decision.get("outcome"),
        "policy_applied": decision.get("policy_applied"),
        # unified merge §6.11: a failed planner replaced by the labelled deterministic plan (additive)
        **({"fallback": decision["planner_fallback"]} if decision.get("planner_fallback") else {}),
    }


def decision_of(result: AnswerResult) -> dict[str, Any]:
    return (result.trace or {}).get("decision") or (result.retrieval.trace or {}).get("decision") or {}


def planner_failure(result: AnswerResult) -> dict[str, Any] | None:
    """``{outcome, failure, policy_applied, stage}`` when planning failed (the decision agent raised
    and its ``on_failure`` policy produced the decision), else ``None``."""
    decision = decision_of(result)
    if not decision.get("failure"):
        return None
    return {k: decision.get(k) for k in ("outcome", "failure", "policy_applied", "stage", "kind")}


def _arm_panel(hits: list[Hit], score_key: str) -> list[dict[str, Any]]:
    return [{"rank": i, "title": h.title or h.document_id, "url": h.url or "", score_key: h.score, "text": h.text[:PANEL_TEXT_CHARS], "record_id": h.document_id} for i, h in enumerate(hits, start=1)]


def _case_panel(hits: list[Hit]) -> list[dict[str, Any]]:
    """Segment-arm hits as the legacy ``cases.results`` panel (one row per parent record; the best
    segment's text and kind)."""
    out = []
    for i, h in enumerate(hits, start=1):
        first = next((p for p in h.provenance if p.get("segment_kind") or p.get("segment_id")), {})
        out.append({"rank": i, "ticket_id": h.document_id, "record_id": h.document_id, "subject": h.title or h.document_id, "module": "", "segment_type": str(first.get("segment_kind") or ""), "text": h.text[:PANEL_TEXT_CHARS], "url": h.url or "", "score": h.score})
    return out


CONTRIBUTORS = "contributor_ids"


def _cell(column: str, value: Any) -> Any:
    if column == CONTRIBUTORS and isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)  # the record ids behind a row, one cell
    return value


def _flat_row(row: Any) -> Any:
    """A SQL-surface row envelope ``{"values": {...}, "contributor_ids": [...], ...}`` -> its column
    mapping (plus ``contributor_ids``); evidence bookkeeping keys are not columns."""
    if isinstance(row, dict) and isinstance(row.get("values"), dict):
        flat = dict(row["values"])
        if row.get("contributor_ids") is not None:
            flat["contributor_ids"] = row["contributor_ids"]
        return flat
    return row


def _row_table(columns: list[str], rows: list[Any]) -> tuple[list[str], list[list[Any]]]:
    """Rows given as ``{column: value}`` mappings -> value lists in ``columns`` order (``columns``
    defaults to the keys in first-seen order). A key no column names (``contributor_ids`` added by
    the SQL surface) becomes a trailing column; ``contributor_ids`` cells are comma-joined ids."""
    rows = [_flat_row(r) for r in rows]
    ordered = list(dict.fromkeys(columns))
    for row in rows:
        if isinstance(row, dict):
            ordered.extend(k for k in row if k not in ordered)
    table = []
    for row in rows:
        if isinstance(row, dict):
            table.append([_cell(c, row.get(c)) for c in ordered])
        else:
            values = list(row)
            table.append([_cell(c, v) for c, v in zip(ordered, values)] + [None] * (len(ordered) - len(values)))
    return ordered, table


def sql_block(aggregates: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    """The first aggregate as the legacy SQL panel; ``generated`` is real SQL text only when the
    aggregate carries it (typed aggregates are labelled as such, never presented as SQL)."""
    for aggregate in aggregates:
        columns, rows = aggregate.get("columns"), aggregate.get("rows")
        if rows and any(isinstance(r, dict) for r in rows):
            columns, rows = _row_table(list(columns or []), rows)
        if not columns and aggregate.get("groups"):
            groups = aggregate["groups"]
            keys = list(groups[0]["key"]) if groups else []
            columns = [*keys, "count"]
            rows = [[g["key"].get(k) for k in keys] + [g["count"]] for g in groups]
        if not columns and aggregate.get("value") is not None and not isinstance(aggregate.get("value"), (dict, list)):
            columns, rows = [aggregate.get("operation") or "value"], [[aggregate["value"]]]
        if not columns:
            continue
        sql = aggregate.get("sql")
        note = None
        if not sql:
            sql = f"-- typed {aggregate.get('operation') or 'aggregate'} (no SQL text): {str(aggregate.get('rendering') or '')[:300]}"
            note = "sql.generated describes a typed aggregate; the package ran no SQL text"
        return {"generated": sql, "columns": list(columns), "rows": [list(r) for r in (rows or [])][:500], "exactness": aggregate.get("exactness")}, note
    return None, None


REVIEW_REASONS = {
    "sufficient": "the reviewer judged the evidence sufficient",
    "no_actionable_gap": "the reviewer found a gap but no follow-up search to run",
    "repeated_query": "the reviewer's follow-up repeated an earlier query; review stopped",
    "hop_limit": "the review hop limit was reached",
    "budget_exhausted": "the run budget ended the review",
    "replan_abstained": "the follow-up could not be planned; review stopped",
}


def _dag_steps(round_: dict[str, Any], round_index: int, question: str, previous: list[str]) -> list[dict[str, Any]]:
    """One execution round as legacy DAG steps: the steps of a round run in parallel (one stage);
    a review hop's round depends on every step of the round before it."""
    out = []
    for i, step in enumerate(round_.get("steps") or [], start=1):
        arm = step.get("arm")
        query = step.get("query")
        out.append(
            {
                "step": f"r{round_index}.s{i}",
                "arm": ARM_MODES.get(arm, arm),
                "depends_on": list(previous),
                "parallel_group": round_index,
                "input_from": ["review"] if previous else [],
                "enriched_query": query if query and query != question else None,
                "n_results": int(step.get("documents") or 0) + (1 if step.get("aggregate") else 0),
                "error": "incomplete" if step.get("incomplete") else None,
                "seconds": step.get("seconds"),
            }
        )
    return out


def agentic_block(result: AnswerResult, question: str) -> dict[str, Any]:
    """The legacy agentic envelope from the package trace: ``execution_dag`` = the first round's plan
    steps (one parallel stage), ``grading`` = the review stage's verdict and stop reason, and the
    ``corrective`` re-run = the review hops' rounds. Without a review stage, grading is the
    generator's own sufficiency flag (said so in ``reason``). ``memory``/``skill`` have no package
    equivalent and stay empty (reported in ``compat.unsupported``)."""
    trace = result.trace or {}
    rounds = trace.get("rounds") or []
    dag = _dag_steps(rounds[0], 1, question, []) if rounds else []
    hops: list[dict[str, Any]] = []
    previous = [s["step"] for s in dag]
    for index, round_ in enumerate(rounds[1:], start=2):
        steps = _dag_steps(round_, index, question, previous)
        hops.extend(steps)
        previous = [s["step"] for s in steps] or previous
    review = trace.get("review")
    if review:
        stop = str(review.get("stop_reason") or "")
        verdicts = [r.get("sufficient") for r in review.get("rounds") or [] if r.get("sufficient") is not None]
        sufficient = stop == "sufficient" or (bool(verdicts) and verdicts[-1] is True)
        reason = REVIEW_REASONS.get(stop, stop or "review ran")
        executed = int(review.get("hops_executed") or 0)
        if executed:
            reason += f"; {executed} follow-up search hop{'s' if executed != 1 else ''} executed"
    else:
        sufficient = not result.answer.insufficient_evidence
        reason = "no review stage in this pipeline; grading is the answer generator's evidence sufficiency flag"
    corrective = None
    if hops:
        queries = [r.get("query") for r in rounds[1:] if r.get("query")]
        corrective = {"reformulated_query": str(queries[-1] if queries else ""), "reason": reason, "trace": hops, "hops": len(rounds) - 1}
    return {
        "execution_dag": dag,
        "grading": {"sufficient": sufficient, "reason": reason, "corrective": corrective},
        "memory": {"recalled": False, "matched_signature": "", "similarity": 0.0, "reward_written": 0},
        "skill": {"used": False, "name": "", "match_score": 0.0, "created": None},
        "arms_used": legacy_modes([s["arm"] for s in [*dag, *hops]]),
    }


def qa_envelope(
    *,
    profile: DatasetProfile,
    question: str,
    result: AnswerResult,
    evidence_items: list[dict[str, Any]],
    legacy_mode: str,
    answer_model: str | None,
    planner_model: str | None,
    embedder: dict[str, Any],
    selection: dict[str, Any],
    wall_seconds: float,
    warnings: list[str],
    unsupported: dict[str, str],
    session_cache: dict[str, Any] | None,
    context_used: bool,
    channel: str | None = None,
    stages: dict[str, int] | None = None,
) -> dict[str, Any]:
    citations_all = number_evidence(profile, evidence_items, result, channel)
    numbers = {c["record_id"]: c["id"] for c in citations_all}
    answer_text = renumber(result.answer.answer, numbers)
    cited = [numbers[c] for c in result.answer.citations if c in numbers]
    citations = citations_all if profile.citations == "evidence" else [c for c in citations_all if c["id"] in set(cited)]
    unsupported = {**ALWAYS_UNSUPPORTED, **unsupported}
    approximations: dict[str, str] = {}
    envelope: dict[str, Any] = {
        "question": question,
        "answer": answer_text,
        "citations": citations,
        "plan": plan_block(result, planner_model),
        "timings": timings(result, wall_seconds, selection),
        "dataset": profile.id,
        "answer_model": answer_model,
        "mode": legacy_mode,
        "engine": "hybrid",
        "bundle_asset_id": result.retrieval.generation,
        "planner_policy": ((result.trace or {}).get("decision") or {}).get("policy_applied"),
        "contextUsed": context_used,
        "delivered_evidence": None,
        "delivered_evidence_chars": ((result.trace or {}).get("evidence") or {}).get("evidence_characters"),
        "insufficient_evidence": result.answer.insufficient_evidence,
    }
    if not selection.get("rerank"):
        unsupported["timings.rerank_ms"] = "no reranking stage configured; reported as 0"
    elif rerank_seconds(selection) is None:
        unsupported["timings.rerank_ms"] = "reranking is configured but did not run for this answer; reported as 0"
    arm_hits = result.retrieval.arm_hits
    if arm_hits.get("dense"):
        envelope["dense"] = {"query": question, "model": embedder.get("model"), "dim": embedder.get("dimensions"), "results": _arm_panel(arm_hits["dense"], "dense_score")}
    if arm_hits.get("lexical"):
        envelope["lexical"] = {"query": question, "results": _arm_panel(arm_hits["lexical"], "bm25_score")}
        approximations["lexical.results.bm25_score"] = "the package lexical arm score (backend-specific), not necessarily BM25"
    if arm_hits.get("graph"):
        envelope["graph"] = {"operation": "graph_arm", "cypher": "", "nodes": [], "edges": [], "paths": [], "records": _arm_panel(arm_hits["graph"], "score")}
        unsupported["graph.nodes"] = "the answer result carries graph hits, not a node/edge export"
    if arm_hits.get("segment"):
        envelope["cases"] = {"query": question, "results": _case_panel(arm_hits["segment"])}
        approximations["cases.results.module"] = "no module field in the package segment index; empty"
    else:
        unsupported["cases"] = "no case-segment evidence in this answer (segment arm not configured or returned nothing)"
    if arm_hits.get("community"):
        unsupported["community"] = "community summaries are not returned (ungrounded summaries are refused)"
    else:
        unsupported["community"] = "no community evidence in this answer"
    ordering = selection.get("ordering") or {}
    envelope["fusion"] = {
        "algorithm": ordering.get("policy") if isinstance(ordering, dict) else str(ordering),
        "k": selection.get("rrf_k") or 60,
        "results": [{"rank": i, "title": h.title or h.document_id, "url": h.url or "", "fused_score": h.score, "modes": legacy_modes(h.arms), "record_id": h.document_id} for i, h in enumerate(result.retrieval.hits, start=1)],
    }
    sql, note = sql_block(result.retrieval.aggregates)
    if sql is not None:
        envelope["sql"] = sql
        if note:
            approximations["sql.generated"] = note
    if stages:
        envelope["timings"]["stages"].update({f"adapter:{k}": v for k, v in stages.items()})
    if legacy_mode == "agentic":
        envelope["agentic"] = agentic_block(result, question)
        unsupported["agentic.memory"] = "no cross-request reasoning memory in the package"
        unsupported["agentic.skill"] = "no learned skills in the package"
    session_answer = (result.trace or {}).get("evidence_answer")
    if session_answer is not None:
        envelope["plan"]["selected_modes"] = ["session_cache"]
        envelope["plan"]["reason"] = "answered from the session's prior evidence (re-read by id; no new search)"
        envelope["plan"]["planner"] = "session_cache"
    if session_cache is not None:
        envelope["session_cache"] = session_cache
        if not session_cache.get("enabled"):
            unsupported["session_cache.answer_from_session"] = "session follow-up answering is not enabled for this dataset (profile session_cache.enabled); every turn runs a full search"
        unsupported["session_cache.gate"] = "follow-ups are recognised by deterministic cues, not a model gate; unclear turns run a full search"
        unsupported["session_cache.standalone_question"] = "a follow-up that needs a full search runs as asked (no model rewrite into a standalone question)"
    failed = planner_failure(result)
    if failed is not None:
        warnings = list(warnings)
        if failed.get("outcome") == "abstain":
            # nothing was searched: never presented as a grounded answer
            envelope["insufficient_evidence"] = True
            warnings.append(f"planning failed ({failed['failure']}): no records were searched; this answer is not grounded in evidence")
        else:
            warnings.append(f"planning failed ({failed['failure']}); retrieval ran under the {failed.get('policy_applied') or failed.get('outcome')} recovery policy")
    if warnings:
        envelope["warnings"] = list(warnings)
    envelope["compat"] = {
        "adapter": "su_multi_arm.compat.legacy_qa",
        "package_version": __version__,
        "generation": result.retrieval.generation,
        "contract": result.contract,
        **({"planner_failure": failed} if failed is not None else {}),
        "violations": [v.model_dump(mode="json") for v in result.violations],
        **({"limitations": list(result.limitations)} if result.limitations else {}),
        "unsupported_constraints": [u.model_dump(mode="json") for u in result.unsupported_constraints],
        "cited": cited,
        "usage": {"model_calls": len((result.trace or {}).get("model_calls") or []), "usd": round(sum(float(c.get("usd") or 0) for c in (result.trace or {}).get("model_calls") or []), 6)},
        "evidence_ids": {str(c["id"]): c["record_id"] for c in citations_all},
        "unsupported": dict(sorted(unsupported.items())),
        "approximations": approximations,
    }
    if session_answer is not None:
        envelope["compat"]["session_evidence"] = session_answer
    coverage = (result.trace or {}).get("source_coverage")
    if coverage is not None:
        # M2b-2 (unified collection): per-source coverage and the citation's source (additive keys)
        envelope["coverage"] = coverage
        for citation in envelope["citations"]:
            citation["source"] = "aggregate" if citation.get("record_type") == "aggregate" else citation.get("record_type")
    return envelope


LINK_KEYS = ("link_id", "other_id", "pair", "tier", "status", "label", "labelled")


def link_status(entries: list[dict[str, Any]]) -> str | None:
    """``reviewed`` when a shown link is accepted (explicit, model- or human-accepted), else
    ``candidate`` (every shown link is an unreviewed candidate, labelled), ``None`` without links."""
    if not entries:
        return None
    return "reviewed" if any(not e.get("labelled") for e in entries) else "candidate"


def attach_links(citations: list[dict[str, Any]], links: dict[str, list[dict[str, Any]]], hidden: set[str]) -> int:
    """Per-citation ``links`` (display fields only; links to hidden records dropped) and
    ``link_status``; returns the number of links shown."""
    shown = 0
    for citation in citations:
        if citation.get("record_type") == "aggregate":
            continue
        entries = [{k: e.get(k) for k in LINK_KEYS} for e in links.get(str(citation.get("record_id")), []) if e.get("other_id") not in hidden]
        if entries:
            citation["links"] = entries
            citation["link_status"] = link_status(entries)
            shown += len(entries)
    return shown
