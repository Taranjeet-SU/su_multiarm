"""Answer a question from a caller-named evidence set (``Engine.answer_from_evidence``).

For follow-up turns that continue an earlier turn (for example "summarise that" on a session): the
caller passes the record ids of the earlier turn's packed evidence, in packing order. The engine

1. re-reads those records from the catalog at the pinned generation **within the principal's access
   scope** (ids that no longer exist or are out of scope are dropped and listed, never guessed);
2. packs them with the configured evidence budget, in the given order (so citation numbering of
   the earlier turn is kept when every record is still there);
3. generates with the ``generator`` role under the answer contract, exactly like ``answer``.

No decision agent, arm or review stage runs, and nothing earlier than the records themselves is
reused: no earlier answer text and no earlier aggregate (aggregates are listed as not reused). The
result's ``trace.evidence_answer`` records the provenance (requested / used / missing ids and the
caller-supplied provenance fields), and ``trace.decision.policy_applied`` is ``evidence_answer``. A
generator that finds the evidence insufficient says so through ``Answer.insufficient_evidence``;
what the caller does then (e.g. run a full search) is the caller's decision, recorded by the caller.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from ..models import AnswerResult, Hit, SearchRequest

if TYPE_CHECKING:  # pragma: no cover
    from ..engine import Engine
    from .acl import Principal

MAX_EVIDENCE_IDS = 200
POLICY = "evidence_answer"
PROVENANCE_KEYS = {"kind", "anchor", "anchor_turn", "session", "reason", "reused_aggregates"}


def _text(document: Any) -> str:
    return "\n".join(v for v in document.fields.values() if v)


def answer_from_evidence(
    engine: "Engine",
    request: SearchRequest | str,
    evidence_ids: list[str],
    principal: "Principal | None" = None,
    session_id: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> AnswerResult:
    from ..decision.base import Decision, DecisionRequest, Plan, RetrievalStep
    from ..engine import _Retrieval
    from ..pipeline.spec import RequestRejected
    from ..selection import Selected

    if isinstance(request, str):
        request = SearchRequest(query=request)
    ids = list(dict.fromkeys(str(i) for i in evidence_ids))
    if not ids:
        raise RequestRejected("answer_from_evidence needs at least one evidence id")
    if len(ids) > MAX_EVIDENCE_IDS:
        raise RequestRejected(f"answer_from_evidence accepts at most {MAX_EVIDENCE_IDS} evidence ids")
    extra = sorted(set(provenance or {}) - PROVENANCE_KEYS)
    if extra:
        raise RequestRejected(f"unknown provenance keys {extra}; valid: {sorted(PROVENANCE_KEYS)}")
    pipe, mode = engine._choose(request)
    engine._check_request(request)
    started = time.perf_counter()
    ctx, pinned, session, info, request = engine._begin(principal, session_id, request)
    outcome: AnswerResult | None = None
    kept: list[str] = []
    try:
        found = {d.id: d for d in engine.documents_in(ctx, ids)}
        kept = [i for i in ids if i in found]
        missing = [i for i in ids if i not in found]
        hits = {
            rid: Hit(
                document_id=rid,
                title=found[rid].title,
                source=found[rid].source,
                url=found[rid].url,
                text=_text(found[rid]),
                score=float(len(kept) - n),
                arms=["evidence"],
                provenance=[{"kind": POLICY}],
            )
            for n, rid in enumerate(kept)
        }
        note = "answered from caller-named evidence re-read by id; no retrieval ran"
        decision = Decision(
            outcome="decided",
            plan=Plan(steps=[RetrievalStep(arm="exact_id", query=request.query)]),  # describes the id lookup; not executed as an arm
            agent=POLICY,
            policy_applied=POLICY,
            note=note,
        )
        state = _Retrieval(
            request=request,
            decision_request=DecisionRequest(query=request.query, record_context=dict(request.record_context)),
            capabilities=engine.capabilities(pipe.name),
            decision=decision,
            pipe=pipe,
            mode=mode,
            hits_by_id=hits,
            ordered=[Selected(record_id=rid, score=hits[rid].score, arms=["evidence"], source=hits[rid].source, reasons=[f"evidence:{n}"]) for n, rid in enumerate(kept, start=1)],
        )
        ctx.trace.event(POLICY, requested=len(ids), found=len(kept), missing=len(missing))
        evidence, pack_trace = engine._pack(ctx, state)
        answer = engine._generate(ctx, state, evidence)
        checked = pipe.contract.enforce(
            answer,
            evidence_ids={row["id"] for row in evidence},
            aggregates=[],
            intents=decision.intents,
            unsupported=[],
            coverage=engine._coverage(pinned[1]),
            trace=ctx.trace,
            question=request.query,
            targets=state.capabilities.structured_targets(),
            passages=[row.get("passage", "") for row in evidence if row.get("source") != "structured"],
        )
        result = engine._result(ctx, state, started, 0.0)
        packed = [row["id"] for row in evidence]
        trace: dict[str, Any] = {
            "seconds": time.perf_counter() - started,
            "rounds": [],
            "stop_reason": "single_pass",
            "review": None,
            "decision": result.trace["decision"],
            "pipeline": pipe.name,
            "mode": mode,
            "selection": {},
            "model_calls": engine._model_calls(ctx),
            "evidence": pack_trace,
            "contract": {"status": checked.contract, "violations": [v.model_dump(mode="json") for v in checked.violations], **({"limitations": list(checked.limitations)} if checked.limitations else {})},
            "trace_id": ctx.trace.id,
            "profile": engine.config.profile,
            "evidence_answer": {
                **(provenance or {}),
                "requested_ids": ids,
                "packed_ids": packed,
                "missing_ids": missing,
                "reused_aggregates": False,
            },
        }
        outcome = AnswerResult(
            answer=checked.answer,
            retrieval=result,
            trace=trace,
            unsupported_constraints=list(checked.unsupported_constraints),
            coverage=checked.coverage,
            violations=list(checked.violations),
            contract=checked.contract,
            limitations=list(checked.limitations),
        )
    finally:
        engine._finish(ctx, session, info, request.query if outcome is not None else None, kept if outcome is not None else [], ctx.trace.id if outcome is not None else None)
    if info is not None:
        outcome.trace["session"] = info
    return outcome
