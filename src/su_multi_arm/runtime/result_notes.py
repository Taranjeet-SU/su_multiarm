"""Model-written relevance notes for search results (opt-in; one ``generator`` call per page).

``result_notes(engine, query, items, principal)`` sends the query and each result's id, title and
text excerpt to the ``generator`` role and asks for at most two sentences per result, from that
result's text only. Guarantees:

* metered like every model call: the run budget (``budgets``), the price table and the
  ``serving.answers`` spend ledger (a refused reservation raises; nothing is written);
* every note is attributed to the result it describes: a note naming an id that is not on the page,
  a duplicate or an empty note is dropped and listed in ``dropped_ids`` (never re-attributed);
* the notes are labelled model-written by the caller; they never replace the result text and are
  not evidence.

No retry, no repair and no fallback: a provider or schema failure raises (the caller decides).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import Field

from ..models import StrictModel

if TYPE_CHECKING:  # pragma: no cover
    from ..engine import Engine
    from .acl import Principal

STAGE = "result_notes"
MAX_ITEMS = 50
NOTES_PROMPT = (
    "You annotate search results. For each result, write at most two short sentences saying what the "
    "result covers and whether it is relevant to the query. Use only that result's own title and text; "
    "do not add facts, numbers or names that are not in it. Reply with JSON only: "
    '{"notes": [{"id": "<result id>", "note": "<text>"}]}, one entry per result id, ids copied exactly.'
)


class ResultNote(StrictModel):
    id: str = Field(min_length=1, max_length=512)
    note: str = Field(max_length=4000)


class ResultNotes(StrictModel):
    notes: list[ResultNote] = Field(default_factory=list, max_length=MAX_ITEMS * 2)


def result_notes(
    engine: "Engine",
    query: str,
    items: list[dict[str, Any]],
    principal: "Principal | None" = None,
    *,
    snippet_chars: int = 500,
    note_chars: int = 300,
) -> dict[str, Any]:
    """``{"notes": {id: text}, "dropped_ids": [...], "model": str, "model_calls": [...]}``."""
    from ..pipeline.spec import RequestRejected
    from ..providers import RunBudgetMeter

    generator = engine.models.get("generator")
    role = engine.config.models.roles.generator
    if generator is None or role is None:
        raise RequestRejected("result notes need a configured generator role")
    if not items:
        return {"notes": {}, "dropped_ids": [], "model": role.model, "model_calls": []}
    if len(items) > MAX_ITEMS:
        raise RequestRejected(f"result notes accept at most {MAX_ITEMS} results")
    ctx = engine._context_for(principal)
    payload = {
        "query": query,
        "results": [{"id": str(i["id"]), "title": str(i.get("title") or ""), "text": str(i.get("text") or "")[:snippet_chars]} for i in items],
    }
    reply = generator.call(NOTES_PROMPT, payload, ResultNotes, RunBudgetMeter(ctx, STAGE, role.price_key()), STAGE)
    allowed = {str(i["id"]) for i in items}
    notes: dict[str, str] = {}
    dropped: list[str] = []
    for note in reply.notes:
        text = " ".join(note.note.split())
        if note.id in allowed and note.id not in notes and text:
            notes[note.id] = text[:note_chars]
        else:
            dropped.append(note.id)
    ctx.trace.event(STAGE, results=len(items), notes=len(notes), dropped=len(dropped))
    return {"notes": notes, "dropped_ids": dropped, "model": role.model, "model_calls": engine._model_calls(ctx)}
