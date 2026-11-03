"""Span-anchored grounding: locate a model-supplied quote in source text through an offset map.

The stored quote is always the exact source substring ``source[start:end]``. Normalisation (NFKC,
casefold, whitespace collapse) is used only to *find* the span; nothing normalised is ever stored.
A quote that cannot be located at the ``exact`` or ``normalised`` level is not grounded and the
caller fails closed. No fuzzy or similarity acceptance exists here by design.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Literal

Step = Literal["nfkc", "casefold", "whitespace"]
MatchLevel = Literal["exact", "normalised", "relocated"]
Ambiguous = Literal["reject", "first"]


@dataclass(frozen=True)
class Located:
    start: int
    end: int
    text: str
    match_level: MatchLevel
    occurrences: int  # candidate matches found at the level that succeeded


def _groups(source: str):
    """Yield (start, end) of each base character together with its trailing combining marks."""
    start = 0
    n = len(source)
    while start < n:
        end = start + 1
        while end < n and unicodedata.combining(source[end]):
            end += 1
        yield start, end
        start = end


def normalise(source: str, steps: list[Step]) -> tuple[str, list[int], list[int]]:
    """Return ``(norm_text, starts, ends)``.

    ``starts[k]`` / ``ends[k]`` are the source offsets of the group that produced normalised character
    ``k``. Expansions (ligatures, full-width forms, ``ß → ss``) map every output character to the same
    source group; a run of whitespace (including NBSP after NFKC) collapses to one space that spans the
    whole run; combining marks stay attached to their base character.
    """
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    run: list[int] | None = None  # [start, end] of the current whitespace run

    def flush():
        nonlocal run
        if run is not None:
            chars.append(" ")
            starts.append(run[0])
            ends.append(run[1])
            run = None

    for gs, ge in _groups(source):
        piece = source[gs:ge]
        if "nfkc" in steps:
            piece = unicodedata.normalize("NFKC", piece)
        if "casefold" in steps:
            piece = piece.casefold()
        if "whitespace" in steps and piece.isspace():
            if run is None:
                run = [gs, ge]
            else:
                run[1] = ge
            continue
        flush()
        for ch in piece:
            chars.append(ch)
            starts.append(gs)
            ends.append(ge)
    flush()
    return "".join(chars), starts, ends


def _find_all(haystack: str, needle: str) -> list[int]:
    positions = []
    pos = haystack.find(needle)
    while pos != -1:
        positions.append(pos)
        pos = haystack.find(needle, pos + 1)
    return positions


def _on_group_boundaries(i: int, j: int, starts: list[int]) -> bool:
    """A normalised match must not begin or end inside one expanded source group."""
    begins = i == 0 or starts[i] != starts[i - 1]
    finishes = j == len(starts) or starts[j] != starts[j - 1]
    return begins and finishes


def locate(quote: str, source: str, steps: list[Step], ambiguous: Ambiguous = "reject") -> Located | None:
    """Locate ``quote`` in ``source``; return the exact source span or ``None``.

    Levels, tried in order: ``exact`` (verbatim substring) then ``normalised`` (match after the
    declared normalisation steps, mapped back through the offset map). Leading and trailing
    whitespace of the quote is ignored. More than one candidate at a level is ``ambiguous``:
    ``reject`` returns ``None``; ``first`` takes the earliest candidate and reports the count.
    """
    q = quote.strip()
    if not q:
        return None
    positions = _find_all(source, q)
    if positions and (len(positions) == 1 or ambiguous == "first"):
        start = positions[0]
        return Located(start, start + len(q), q, "exact", len(positions))
    if positions:
        return None
    if not steps:
        return None
    norm_source, starts, ends = normalise(source, steps)
    norm_quote = normalise(q, steps)[0].strip()
    if not norm_quote:
        return None
    candidates = [
        i for i in _find_all(norm_source, norm_quote) if _on_group_boundaries(i, i + len(norm_quote), starts)
    ]
    if not candidates or (len(candidates) > 1 and ambiguous == "reject"):
        return None
    i = candidates[0]
    j = i + len(norm_quote)
    start, end = starts[i], ends[j - 1]
    text = source[start:end]
    if normalise(text, steps)[0].strip() != norm_quote:  # offset-map invariant; never store a drifted span
        return None
    return Located(start, end, text, "normalised", len(candidates))


def contains(outer: tuple[int, int], inner: tuple[int, int]) -> bool:
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def locate_in_record(
    quote: str,
    primary_field: str,
    fields: dict[str, str],
    steps: list[Step],
    ambiguous: Ambiguous = "reject",
) -> tuple[str, Located] | None:
    """Locate ``quote`` in its own field first, then in the other fields of the same record.

    A match in ``primary_field`` keeps its own level (``exact``/``normalised``). A match found only in
    another field is reported with ``match_level="relocated"`` together with that field's name: the
    stored span is still verbatim source text, just not from the field the extractor was shown. Other
    fields are tried in sorted order; more than one *field* containing the quote is ambiguous and
    follows the ``ambiguous`` policy (``reject`` -> ``None``, ``first`` -> the first field).
    """
    primary = fields.get(primary_field, "")
    own = locate(quote, primary, steps, ambiguous)
    if own is not None:
        return primary_field, own
    candidates: list[tuple[str, Located]] = []
    for name in sorted(fields):
        if name == primary_field:
            continue
        found = locate(quote, fields[name], steps, ambiguous)
        if found is not None:
            candidates.append((name, found))
    if not candidates or (len(candidates) > 1 and ambiguous == "reject"):
        return None
    name, found = candidates[0]
    return name, Located(found.start, found.end, found.text, "relocated", found.occurrences)
