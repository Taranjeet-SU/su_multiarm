"""``extraction.patterns`` (extractor ``regex``) expressed as pattern-library entries (addendum O10).

Each ``{type: regex}`` pair is equivalent to one ``annotate`` entry with a single named group around
the whole expression and ``(?i)`` (the ``regex`` extractor matches case-insensitively). In this
release the ``regex`` extractor still runs on Python ``re``, so its results and the index fingerprints
of existing configs are unchanged; ``regex_entries`` is the mapping the 2.1 switch will use, and it
fails (``PatternLibraryError``) for an expression outside the RE2 subset or the ReDoS rules instead of
changing what it matches.
"""

from __future__ import annotations

from .engine import validate_regex
from .library import PatternEntry, PatternLibraryError


def regex_entries(patterns: dict[str, str]) -> list[PatternEntry]:
    entries = []
    for index, (kind, expression) in enumerate(sorted(patterns.items())):
        regex = f"(?i)(?P<value>{expression})"
        errors = [i for i in validate_regex(regex, pattern_id=f"regex.{index}") if i.severity == "error"]
        if errors:
            raise PatternLibraryError(f"extraction.patterns[{kind!r}] cannot run on the pattern engine: {errors[0].message}", errors)
        entries.append(
            PatternEntry(
                id=f"regex.{index}",
                kind="annotate",
                status="approved",
                regex=regex,
                entities=[{"group": "value", "type": kind}],
                provenance={"author": "config", "source": "extraction.patterns"},
            )
        )
    return entries
