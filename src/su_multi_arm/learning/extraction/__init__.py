"""LLM fallback extraction for log lines the pattern library cannot parse or parses without a
required field (addendum A2 rev 2 §4.2; slices C1–C4).

- ``slots``: cluster-first: one ``pattern_learner`` call per residue cluster labels the groups of
  the synthesised regex (or proposes a regex); the gate decides; values are group spans.
- ``fill``: field gaps: a ``fix`` proposal per systematic gap, then capped per-line quotes.
- ``lines``: the capped per-line tail; every quote located in its own line or dropped.
- ``cache``: the reply cache (template/line keys; only labels, regex and quotes are reused).
- ``common``: residue heads, provider routes with readiness/capacity probes, the cache-first caller.
- ``run``: one run over sealed partitions, written as overlays (``ingestion/overlay.py``).
"""

from .cache import ReplyCache
from .common import Caller, CachePending, Head, HttpProbe, Outcome, Route
from .replies import ClusterExtractionReply, GroupLabel, LineBatchReply, LineExtractionReply, QuotedField
from .run import FallbackSession

__all__ = [
    "CachePending",
    "Caller",
    "ClusterExtractionReply",
    "FallbackSession",
    "GroupLabel",
    "Head",
    "HttpProbe",
    "LineBatchReply",
    "LineExtractionReply",
    "Outcome",
    "QuotedField",
    "ReplyCache",
    "Route",
]
