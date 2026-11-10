"""The ``pattern`` extractor and its library (addendum A1 §3.3–3.4).

- ``engine``: RE2 (``google-re2``) or the explicit ``regex`` development engine, the RE2-subset
  validator and the ReDoS lint;
- ``library``: library files, the multi-file loader with decisions replay, lint and identity;
- ``extractor``: staged line matching, line-scoped facts, header attribute links, residue;
- ``coverage``: line status counts; ``writer``: CAS-safe appends to the system-owned files.
"""

from .engine import LintIssue, PatternEngineUnavailable, PatternTimeout, select_engine, validate_regex
from .extractor import CompiledLibrary, LineResult, PatternExtractor
from .library import Decision, Library, PatternEntry, PatternLibraryError, load_library

__all__ = [
    "CompiledLibrary",
    "Decision",
    "Library",
    "LineResult",
    "LintIssue",
    "PatternEngineUnavailable",
    "PatternEntry",
    "PatternExtractor",
    "PatternLibraryError",
    "PatternTimeout",
    "load_library",
    "select_engine",
    "validate_regex",
]
