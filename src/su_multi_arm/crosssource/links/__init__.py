"""Cross-source links (unified merge plan §6.9; increment M3, lane LD).

* ``signature_mentions`` — rule-id / rule-title mentions of the log member's error signatures.
* ``builders`` — tier A (explicit references) and tier B (customer + time window + signature for
  ticket <-> episode; signature mention for ticket <-> issue family and family <-> doc; title
  mention for ticket <-> doc); analytics clicks stubbed until M7; tier C is M9.
* ``store`` — the private, append-only, versioned link store with the seven statuses, retraction
  and tombstones, report/export, and the pinned query-time ``LinkView`` (display data only; record
  pages and citations are rendered by the serving lane).
* ``review`` — model review of candidate links through the shared review ledger.

Identity joins only through ``IdentityView`` (matched aliases of the pinned registry). Links never
grant access: callers apply visibility to both records before showing a link.
"""

from .builders import BUILDER_VERSION, BuildResult, Link, LinkSettings, build_links
from .store import DISPLAY, LABELS, STATUSES, LinkState, LinkStore, LinkStoreError, LinkView

__all__ = ["BUILDER_VERSION", "BuildResult", "DISPLAY", "LABELS", "Link", "LinkSettings", "LinkState", "LinkStore", "LinkStoreError", "LinkView", "STATUSES", "build_links"]
