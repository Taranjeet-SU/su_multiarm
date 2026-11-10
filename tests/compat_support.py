"""Synthetic corpus and engines for the paged-search and legacy-QA compatibility tests.

Twelve records in two projects with distinct creation dates, a status and a kind, on the in-memory
backend (or SQLite) with the hash embedder. Generators are the loopback ``FakeChatServer``
(contract-mocked); no network beyond 127.0.0.1 and no paid provider."""

from __future__ import annotations

from typing import Any

from su_multi_arm import Document, Engine

from serving_support import memory_config

PROJECTS = ["Alpha", "Beta"]
STATUSES = ["open", "closed", "pending"]


def corpus() -> list[Document]:
    docs = []
    for i in range(12):
        rid = f"r{i + 1:02d}"
        project = PROJECTS[i % 2]
        status = STATUSES[i % 3]
        kind = "record" if i % 4 else "report"
        text = f"Billing record {rid} about invoices, rounding and region totals for project {project}."
        docs.append(
            Document(
                id=rid,
                dataset="demo",
                kind=kind,
                title=f"Billing {rid}",
                fields={"summary": text, "details": f"Details for {rid}: status {status}. " + "Line items and notes. " * 6},
                metadata={
                    "kind": kind,
                    "project": project,
                    "status": status,
                    "external_id": f"REC-{2000 + i}",
                    "tag": "t1",
                    "created_at": f"2024-{(i % 12) + 1:02d}-{(i * 2) % 27 + 1:02d}T08:00:00+00:00",
                    **({"amount": float(i)} if i % 5 else {}),
                },
                url=None if i % 3 == 0 else f"https://example.org/records/{rid}",
            )
        )
    return docs


def engine(generator: dict[str, Any] | None = None, **overrides: Any) -> Engine:
    eng = Engine(memory_config(generator, **overrides))
    eng.ingest(corpus())
    return eng
