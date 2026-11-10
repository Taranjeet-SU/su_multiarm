"""A synthetic config with the *shape* of a live support deployment (unified merge plan F0, §6.13).

The shape (which blocks are set and how they combine) mirrors a production support config; every
value is invented: field names, table names, paths, prompt, prices and the synthetic corpus it
points at. It combines: sqlite with the ``bm25`` lexical engine and faiss, a kuzu backend in
``mode: attach`` with a mapping file, ``ingestion.attach`` sources pinned by sha256, imported
communities, precomputed embeddings (pinned vectors + keys), an enabled typed projection, the
``typed_query`` arm in ``sql_surface`` mode with a router (``query_writer`` role), the ``segment``
arm (sidecar path), the graph and community arms, an LLM planner with arm cards, an ``agentic``
pipeline with review hops, a 120k-character request evidence ceiling, a USD budget with a price
table and a daily answer spend ledger.

``template_files()`` holds the committed text files (``config.template.yaml`` with ``@sha256:<file>@``
and ``@path:<file>@`` placeholders, mapping, communities, keys, prompt, prices, segment rows).
``materialize(target)`` writes them, builds the binary side files (attach sqlite, ``vectors.npy``)
and writes ``config.yaml`` with the digests and paths filled in. ``@path`` slots become absolute
paths, as the live shape uses there. (Before S0, ``load_config`` resolved ``ingestion.attach[].path``,
``communities.imported.path`` and ``pipeline.arms.segment.path`` against the working directory;
since S0 a relative value resolves against the config's directory like every other path.) The segment
sidecar directory and the kuzu store are *not* built: the config loads and validates
(``load_config``), but an ``Engine`` needs those stores.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

import yaml

from ....extraction.chunking import chunk_document
from .collection import doc_documents, ticket_documents

DIMENSIONS = 1024
CHUNK_SIZE, CHUNK_OVERLAP = 1600, 160
PLACEHOLDER = re.compile(r"@(sha256|path):([A-Za-z0-9_./-]+)@")

SCHEMA_FIELDS = [
    {"name": "source", "type": "enum", "enum_values": ["ticket", "doc"], "listable": True, "groupable": True, "description": "Record source: a support ticket or a documentation page."},
    {"name": "account", "type": "string", "listable": True, "groupable": True, "description": "Customer account name as entered on the ticket (raw text)."},
    {"name": "product", "type": "string", "listable": True, "groupable": True, "description": "Product area of the record."},
    {"name": "service", "type": "string", "listable": True, "groupable": True, "description": "Service family the record concerns."},
    {"name": "status", "type": "string", "listable": True, "groupable": True, "description": "Ticket status at snapshot time."},
    {"name": "priority", "type": "string", "listable": True, "groupable": True, "description": "Ticket priority."},
    {"name": "created_at", "type": "datetime", "sortable": True, "time_basis": "created", "description": "When the ticket was opened (UTC)."},
    {"name": "resolved_at", "type": "datetime", "sortable": True, "time_basis": "resolved", "description": "When the ticket was solved (UTC); null while open."},
    {"name": "updated_at", "type": "datetime", "sortable": True, "time_basis": "updated", "description": "Last update of a documentation page (UTC)."},
    {"name": "ticket_number", "type": "id", "pattern": "ticket:[0-9]{3,6}", "description": "Ticket identifier."},
    {"name": "kg_request_type", "type": "string", "listable": True, "groupable": True, "description": "Request type assigned by the offline ticket analysis."},
    {"name": "kg_outcome", "type": "string", "listable": True, "groupable": True, "description": "Outcome assigned by the offline ticket analysis."},
    {"name": "kg_is_resolved", "type": "boolean", "description": "Whether the offline analysis judged the ticket resolved."},
    {"name": "kg_severity", "type": "string", "listable": True, "groupable": True, "description": "Severity assigned by the offline ticket analysis."},
    {"name": "sla_breached", "type": "boolean", "description": "Whether the response target was missed."},
]
PROJECTION_COLUMNS = ["source", "account", "product", "service", "status", "priority", "created_at", "resolved_at", "kg_request_type", "kg_outcome", "kg_is_resolved", "kg_severity", "sla_breached"]

CONFIG = {
    "schema_version": 2,
    "dataset": "support_fixture",
    "profile": "neutral",
    "backends": {
        "local": {"kind": "sqlite", "path": "state", "lexical_engine": "bm25", "bm25": {"tokenizer": "ascii_alnum"}, "vector_engine": "faiss"},
        "kz": {"kind": "kuzu", "path": "graph.kuzu", "mode": "attach", "attach": {"mapping": "kuzu_mapping.yaml"}},
    },
    "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
    "metadata_schema": {"canonical_unit": "ticket", "fields": SCHEMA_FIELDS},
    "projection": {
        "enabled": True,
        "backend": "local",
        "tables": {"ticket": {"kind": "ticket", "description": "One row per support ticket.", "columns": PROJECTION_COLUMNS,
                              "text_columns": {"subject": {"source": "title", "description": "Ticket subject line (free text)."},
                                               "description": {"source": "fields.description", "description": "Ticket description (free text)."}}}},
    },
    "ingestion": {
        "chunking": {"size": CHUNK_SIZE, "overlap": CHUNK_OVERLAP},
        "attach": [
            {"path": "@path:ticket_insights.sqlite@", "sha256": "@sha256:ticket_insights.sqlite@", "table": "ticket_insights", "key_column": "ticket_id", "document_id": "ticket:{key}",
             "columns": {"request_type": "kg_request_type", "outcome": "kg_outcome", "is_resolved": "kg_is_resolved", "severity": "kg_severity"}},
            {"path": "@path:ticket_insights.sqlite@", "sha256": "@sha256:ticket_insights.sqlite@", "table": "ticket_sla", "key_column": "ticket_id", "document_id": "ticket:{key}",
             "columns": {"breached": "sla_breached"}},
        ],
    },
    "extraction": {"extractors": []},
    "communities": {"strategy": "imported", "imported": {"path": "@path:communities.jsonl@", "sha256": "@sha256:communities.jsonl@", "on_ungrounded": "drop_claim", "extractive_sentences": 3}},
    "request": {"context_keys": ["subject", "description", "account", "product"], "pipelines": ["agentic"], "evidence_chars_max": 120000},
    "models": {"roles": {
        "planner": {"provider": "anthropic", "model": "claude-sonnet-5-5", "api_key_env": "SU_FIXTURE_API_KEY", "max_tokens": 1500, "timeout_seconds": 90, "thinking": "off"},
        "query_writer": {"provider": "anthropic", "model": "claude-sonnet-5-5", "api_key_env": "SU_FIXTURE_API_KEY", "max_tokens": 3000, "timeout_seconds": 90, "thinking": "off"},
        "reviewer": {"provider": "anthropic", "model": "claude-sonnet-5-5", "api_key_env": "SU_FIXTURE_API_KEY", "max_tokens": 1500, "timeout_seconds": 90, "thinking": "off",
                     "capabilities": {"structured_output": "none"}},
        "generator": {"provider": "anthropic", "model": "claude-haiku-4-5", "api_key_env": "SU_FIXTURE_API_KEY", "max_tokens": 2600, "timeout_seconds": 120, "thinking": "off",
                      "measure_first_token": True, "capabilities": {"streaming": True}, "evidence_budget": {"chars": 40000}},
        "embedder": {"provider": "sentence_transformers", "model": "BAAI/bge-m3", "dimensions": DIMENSIONS, "device": "cpu", "batch_size": 32,
                     "precomputed": {"vectors": "vectors.npy", "keys": "precomputed_keys.jsonl", "sha256": "@sha256:vectors.npy@", "keys_sha256": "@sha256:precomputed_keys.jsonl@",
                                     "verify": {"sample": 0}}},
    }},
    "pipeline": {
        "decision": {"kind": "llm", "prompt_file": "planner_prompt.md", "max_steps": 6, "on_step_failure": "partial", "arm_cards": True,
                     "typed_query": {"enabled": True, "repair_attempts": 0, "routing": "router", "router_role": "query_writer"}},
        "generation": {"contract": {"on_violation": "annotate"}},
        "arms": {
            "exact_id": {"alias_fields": ["ticket_number"]},
            "dense": {"backend": "local", "candidates": 400},
            "lexical": {"backend": "local", "candidates": 400},
            "structured": {"backend": "local"},
            "typed_query": {"mode": "sql_surface", "row_limit": 200, "text_backend": "local",
                            "sql_surface": {"role": "query_writer", "repair_attempts": 1, "list_values_up_to": 64}},
            "segment": {"path": "@path:segments@", "candidates": 40, "lexical": True, "max_segments_per_parent": 3},
            "community": {"backend": "local", "claim_hits": True},
            "graph": {"backend": "kz", "hops": 1, "path_evidence": True},
        },
        "selection": {"collapse": "document", "per_arm": 20, "ordering": "reciprocal_rank", "arm_weights": {"graph": 0.25, "segment": 0.25, "community": 0.25},
                      "source_weights": {"doc": 1.5}, "rerank": False,
                      "packing": {"metadata_fields": ["source", "account", "product", "service", "status", "priority", "created_at", "resolved_at", "kg_outcome"],
                                  "metadata_show_null": ["resolved_at"], "metadata_protected": ["source", "status", "created_at"], "metadata_max_share": 0.25}},
    },
    "pipelines": {"agentic": {"review": {"enabled": True, "max_hops": 2}}},
    "serving": {"answers": {"enabled": True, "max_usd_per_day": 3.0, "ledger": "state/answers_spend.sqlite"}},
    "budgets": {"max_llm_calls": 7, "max_usd": 0.25, "deadline_seconds": 240, "price_table": "prices.yaml"},
}

# Synthetic prices (not a vendor's price list): only the shape matters to the plan check tests.
PRICES = {"version": "synthetic-fixture-1", "currency": "USD", "prices": {
    "claude-sonnet-5-5": {"input_per_million": 3.0, "output_per_million": 15.0},
    "claude-haiku-4-5": {"input_per_million": 1.0, "output_per_million": 5.0},
}}
PROMPT = ("You plan retrieval over a synthetic support corpus of tickets and documentation pages.\n"
          "Choose arms from the arm cards, keep steps few, and prefer exact identifiers when the question names one.\n")
MAPPING = {
    "dataset": "support_fixture",
    "any_generation": True,
    "nodes": [
        {"table": "Ticket", "type": "issue", "name": "title", "document": {"column": "id", "pattern": "^(ticket:[0-9]+)$"}, "mention_text": "body"},
        {"table": "Symptom", "type": "symptom"},
        {"table": "Fix", "type": "fix"},
    ],
    "edges": [
        {"table": "HAS_SYMPTOM", "predicate": "has_symptom", "document_pattern": "^(ticket:[0-9]+)", "verified": "verified"},
        {"table": "RESOLVED_BY", "predicate": "resolved_by", "document_pattern": "^(ticket:[0-9]+)", "verified": "verified"},
    ],
}
INSIGHTS = [
    # ticket number, request type, outcome, resolved, severity, sla breached
    (1001, "configuration", "fixed", 1, "medium", 0), (1002, "incident", "fixed", 1, "high", 0), (1003, "incident", "open", 0, "critical", 1),
    (1004, "incident", "open", 0, "high", 0), (1005, "incident", "workaround", 1, "medium", 0), (1006, "incident", "fixed", 1, "low", 0),
]


def _support():
    return [*ticket_documents(), *doc_documents()]


def _communities() -> bytes:
    rows = [
        {"id": "fx-crawl", "label": "Crawler timeouts", "level": 0, "parent_id": None, "members": ["ticket:1001", "ticket:1002", "ticket:1005"],
         "claims": [{"text": "Large sitemaps cause fetch timeouts.", "evidence": [{"document_id": "ticket:1001", "quote": "Crawls of large sitemaps stop with timeouts"}]}],
         "summary": "Crawler timeout tickets.", "stats": {"tickets": 3}},
        {"id": "fx-search", "label": "Search server errors", "level": 0, "parent_id": None, "members": ["ticket:1003", "ticket:1006"],
         "claims": [{"text": "Search returned server errors.", "evidence": [{"document_id": "ticket:1006", "quote": "A few search requests returned search.5xx errors."}]}],
         "summary": "Search error tickets.", "stats": {"tickets": 2}},
    ]
    return "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows).encode()


def _keys() -> bytes:
    lines = []
    for doc in _support():
        if doc.kind != "ticket":
            continue
        for chunk in chunk_document(doc, CHUNK_SIZE, CHUNK_OVERLAP, []):
            lines.append(json.dumps({"document_id": chunk.document_id, "field": chunk.field, "text_sha256": hashlib.sha256(chunk.text.encode()).hexdigest()}))
    return ("\n".join(lines) + "\n").encode()


def _segments() -> bytes:
    rows = []
    for doc in ticket_documents():
        for seq, name in enumerate(("description", "resolution")):
            if doc.fields.get(name):
                rows.append({"id": f"{doc.id}:{name[0]}", "parent_id": doc.id, "kind": name, "text": doc.fields[name], "field": name, "seq": seq})
    return "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows).encode()


def template_files() -> dict[str, bytes]:
    head = "# Synthetic live-shape fixture (unified merge plan F0): invented values; materialize() fills the sha256 placeholders.\n"
    return {
        "config.template.yaml": (head + yaml.safe_dump(CONFIG, sort_keys=False, width=200)).encode(),
        "prices.yaml": (head + yaml.safe_dump(PRICES, sort_keys=False)).encode(),
        "planner_prompt.md": PROMPT.encode(),
        "kuzu_mapping.yaml": (head + yaml.safe_dump(MAPPING, sort_keys=False)).encode(),
        "communities.jsonl": _communities(),
        "precomputed_keys.jsonl": _keys(),
        "segment_rows.jsonl": _segments(),
    }


def _insights_db(path: Path) -> None:
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE ticket_insights(ticket_id TEXT PRIMARY KEY, request_type TEXT, outcome TEXT, is_resolved INTEGER, severity TEXT)")
        con.execute("CREATE TABLE ticket_sla(ticket_id TEXT PRIMARY KEY, breached INTEGER)")
        con.executemany("INSERT INTO ticket_insights VALUES (?,?,?,?,?)", [(str(n), t, o, r, s) for n, t, o, r, s, _b in INSIGHTS])
        con.executemany("INSERT INTO ticket_sla VALUES (?,?)", [(str(n), b) for n, *_rest, b in INSIGHTS])
        con.commit()
    finally:
        con.close()


def _vectors(path: Path, rows: int) -> None:
    import numpy as np

    rng = np.random.default_rng(20261109)
    matrix = rng.standard_normal((rows, DIMENSIONS)).astype(np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    np.save(path, matrix)


def materialize(target: str | Path) -> Path:
    """Write the live-shape config and its side files into ``target``; returns ``config.yaml``."""
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    files = template_files()
    for name, data in files.items():
        (target / name).write_bytes(data)
    _insights_db(target / "ticket_insights.sqlite")
    _vectors(target / "vectors.npy", len(files["precomputed_keys.jsonl"].splitlines()))
    text = files["config.template.yaml"].decode()
    root = target.resolve()

    def fill(match: re.Match) -> str:
        what, name = match.group(1), match.group(2)
        return hashlib.sha256((root / name).read_bytes()).hexdigest() if what == "sha256" else str(root / name)

    text = PLACEHOLDER.sub(fill, text)
    out = target / "config.yaml"
    out.write_text(text)
    return out
