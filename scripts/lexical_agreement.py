"""Lexical cross-engine agreement report (plan §3.5 lexical semantics, Phase 1 acceptance; informational).

Ingests the shipped synthetic examples (``examples/documents.jsonl`` and, through the v1 upgrade path,
``examples/mapped_documents.json`` with ``examples/mapping.yaml``) into the lexical index of every
engine given, chunked once with the ``examples/v2/local.yaml`` chunking recipe so every engine sees
identical chunks, runs a fixed query list (terms, phrase and boolean kinds) and reports, per engine
pair: top-10 chunk-id Jaccard, Kendall tau on the shared items, and membership-only agreement of the
complete match sets. Ranking parity is **not** a contract (plan §3.5); this report is not a gate.

Engines: ``sqlite`` (always; in ``--workdir``), ``postgres``, ``opensearch``, ``elasticsearch`` and
``mongodb``, read from the same ``SU_TEST_*`` variables as the real-service tests
(``tests/services/README.md``). Point them only at the throwaway stack: the script creates and drops
its own random prefixes. An unset variable leaves that engine out (listed in the report); a set
variable whose server fails is an error, never a silent skip.

    python scripts/lexical_agreement.py --workdir ~/su_mutliarm_tests/<agent>/lexical \
        --digests digests.txt --output docs/LEXICAL_AGREEMENT.md
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from statistics import mean
from typing import Any

from su_multi_arm.backends import capabilities as caps
from su_multi_arm.core.config import load_config
from su_multi_arm.core.errors import BackendError
from su_multi_arm.core.types import Chunk, Document, GenerationId, LexicalQuery
from su_multi_arm.extraction.chunking import chunk_document
from su_multi_arm.ingestion.normalize import parse_documents

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
TOP_K = 10
ALL_K = 1000  # larger than the corpus: the complete match set
ENGINES = ["sqlite", "postgres", "opensearch", "elasticsearch", "mongodb"]

# Fixed query list derived from the example corpus vocabulary.
QUERIES: list[tuple[str, LexicalQuery]] = [
    ("t:certificate", LexicalQuery(text="certificate")),
    ("t:certificates", LexicalQuery(text="certificates")),
    ("t:exemption certificate", LexicalQuery(text="exemption certificate")),
    ("t:rounding", LexicalQuery(text="rounding")),
    ("t:ERR-204", LexicalQuery(text="ERR-204")),
    ("t:ERR-204 rounding", LexicalQuery(text="ERR-204 rounding")),
    ("t:content source", LexicalQuery(text="content source")),
    ("t:decimal precision", LexicalQuery(text="decimal precision")),
    ("t:effective date", LexicalQuery(text="effective date")),
    ("t:renewed certificate", LexicalQuery(text="renewed certificate")),
    ("t:California", LexicalQuery(text="California")),
    ("t:connector token", LexicalQuery(text="connector token")),
    ("t:invoice totals", LexicalQuery(text="invoice totals")),
    ("t:expired", LexicalQuery(text="expired")),
    ("t:applying", LexicalQuery(text="applying")),
    ("p:exemption certificate", LexicalQuery(kind="phrase", text="exemption certificate")),
    ("p:content source", LexicalQuery(kind="phrase", text="content source")),
    ("p:decimal precision", LexicalQuery(kind="phrase", text="decimal precision")),
    ("p:effective date", LexicalQuery(kind="phrase", text="effective date")),
    ("b:certificate -expired", LexicalQuery(kind="boolean", text="certificate", must=["certificate"], must_not=["expired"])),
    ("b:rounding -invoice", LexicalQuery(kind="boolean", text="rounding", must=["rounding"], must_not=["invoice"])),
    ("b:source +content -SearchDesk", LexicalQuery(kind="boolean", text="source", must=["content"], must_not=["SearchDesk"])),
    ("b:renewed -certificate", LexicalQuery(kind="boolean", text="renewed", must=["renewed"], must_not=["certificate"])),
]


def corpus() -> tuple[list[Document], list[Chunk]]:
    local = load_config(EXAMPLES / "v2" / "local.yaml", warn=False)
    mapped_cfg = load_config(EXAMPLES / "mapping.yaml", warn=False)
    records = [json.loads(line) for line in (EXAMPLES / "documents.jsonl").read_text().splitlines() if line.strip()]
    docs = parse_documents(records, local)
    docs += parse_documents(json.loads((EXAMPLES / "mapped_documents.json").read_text()), mapped_cfg)
    ids = [d.id for d in docs]
    if len(set(ids)) != len(ids):
        raise SystemExit(f"duplicate document ids in the examples: {ids}")
    docs = [d.model_copy(update={"dataset": "demo"}) for d in docs]
    chunking = local.ingestion.chunking
    chunks = [c for d in docs for c in chunk_document(d, chunking.size, chunking.overlap, chunking.field_order)]
    return docs, chunks


def build(kind: str, workdir: Path, prefix: str) -> Any | None:
    if kind == "sqlite":
        from su_multi_arm.backends.sqlite import SqliteBackend

        return SqliteBackend(workdir / prefix)
    if kind == "postgres":
        if not (dsn := os.environ.get("SU_TEST_POSTGRES_DSN")):
            return None
        from su_multi_arm.backends.postgres import PostgresBackend

        return PostgresBackend(dsn, prefix, timeout_seconds=30)
    if kind in {"opensearch", "elasticsearch"}:
        url = os.environ.get("SU_TEST_OPENSEARCH_URL" if kind == "opensearch" else "SU_TEST_ELASTICSEARCH_URL")
        if not url:
            return None
        if kind == "opensearch":
            from su_multi_arm.backends.opensearch import OpenSearchBackend as Backend
        else:
            from su_multi_arm.backends.elasticsearch import ElasticsearchBackend as Backend
        return Backend(url, index_prefix=prefix, knn_method="exact", timeout_seconds=30)
    if kind == "mongodb":
        if not (uri := os.environ.get("SU_TEST_MONGO_URI")):
            return None
        from su_multi_arm.backends.mongodb import MongoBackend

        return MongoBackend(uri, "su_lexical_agreement", prefix, timeout_seconds=30)
    raise SystemExit(f"unknown engine {kind}")


def cleanup(kind: str, backend: Any) -> None:
    if kind in {"postgres", "mongodb"}:
        backend.drop_all()
    if kind in {"opensearch", "elasticsearch"}:
        rows = backend.transport.request("GET", f"/_cat/indices/{backend.prefix}_*", params={"format": "json", "h": "index"})
        names = [row["index"] for row in rows or []]
        if names:  # listed by name: Elasticsearch refuses wildcard deletes
            backend.transport.request("DELETE", "/" + ",".join(names))
    if kind == "mongodb":
        backend.client.close()
    elif hasattr(backend, "close"):
        backend.close()


def analyzer(kind: str, backend: Any, gen: GenerationId) -> str:
    if kind == "postgres":
        return backend.analyzer_id(gen)
    return str(caps.flags(kind, "lexical").get("analyzer_id"))


def run_engine(kind: str, backend: Any, gen: GenerationId, docs: list[Document], chunks: list[Chunk]) -> dict[str, Any]:
    backend.catalog.put_documents(gen, docs, chunks)
    backend.lexical.index(gen, chunks, [])
    backend.lexical.refresh(gen)
    out: dict[str, Any] = {"analyzer": analyzer(kind, backend, gen), "results": {}}
    for name, query in QUERIES:
        try:
            top = [h.chunk_id for h in backend.lexical.search(gen, query, None, TOP_K).items]
            every = [h.chunk_id for h in backend.lexical.search(gen, query, None, ALL_K).items]
            out["results"][name] = {"top": top, "all": every}
        except BackendError as exc:  # reported per query, never dropped
            out["results"][name] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return 1.0 if not sa and not sb else len(sa & sb) / len(sa | sb)


def kendall_tau(a: list[str], b: list[str]) -> float | None:
    """Kendall tau-a over the items present in both rankings; None with fewer than two shared items."""
    shared = [x for x in a if x in set(b)]
    if len(shared) < 2:
        return None
    rb = {x: i for i, x in enumerate(b)}
    concordant = discordant = 0
    for x, y in itertools.combinations(shared, 2):  # a-order: x before y
        if rb[x] < rb[y]:
            concordant += 1
        else:
            discordant += 1
    return (concordant - discordant) / (concordant + discordant)


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def compare(runs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for left, right in itertools.combinations(runs, 2):
        jac, taus, member, member_eq, errors = [], [], [], 0, 0
        for name, _ in QUERIES:
            a, b = runs[left]["results"][name], runs[right]["results"][name]
            if "error" in a or "error" in b:
                errors += 1
                continue
            jac.append(jaccard(a["top"], b["top"]))
            if (tau := kendall_tau(a["top"], b["top"])) is not None:
                taus.append(tau)
            member.append(jaccard(a["all"], b["all"]))
            member_eq += set(a["all"]) == set(b["all"])
        rows.append(
            {
                "pair": f"{left} / {right}",
                "queries": len(jac),
                "errors": errors,
                "jaccard_top10": mean(jac) if jac else None,
                "tau": mean(taus) if taus else None,
                "tau_n": len(taus),
                "member_jaccard": mean(member) if member else None,
                "member_identical": member_eq,
            }
        )
    return rows


def git_commit() -> str:
    try:
        sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", "src"], capture_output=True, text=True, check=True).stdout.strip()
        return sha + (" (src/ modified)" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def report(runs: dict[str, dict[str, Any]], missing: list[str], docs: int, chunks: int, digests: str | None) -> str:
    lines = [
        "# Lexical cross-engine agreement",
        "",
        "Generated by `scripts/lexical_agreement.py` (plan §3.5, Phase 1 acceptance). **Informational, not a gate:**",
        "ranking parity across engines is deliberately not a contract. The corpus is the shipped synthetic",
        f"examples ({docs} documents, {chunks} chunks), so these numbers describe analyzer and query-semantics",
        "differences on a tiny fixture and say nothing about relevance or about larger corpora.",
        "",
        f"- Commit: `{git_commit()}`",
        "- Engines: " + ", ".join(f"{k} (`{v['analyzer']}`)" for k, v in runs.items()),
    ]
    if missing:
        lines.append(f"- Not run (variable unset): {', '.join(missing)}")
    if digests:
        lines += ["- Images:", *[f"  - `{line}`" for line in digests.strip().splitlines()]]
    lines += [
        f"- Queries: {len(QUERIES)} (`t:` terms, `p:` phrase, `b:` boolean with must/must_not), no scope.",
        "",
        "Metrics per engine pair, averaged over queries both engines answered:",
        f"`top-10 Jaccard` of chunk ids at k={TOP_K}; `Kendall tau` (tau-a) on the shared top-10 items, over",
        "queries with at least two shared items (`n`); `membership Jaccard` of the complete match sets",
        f"(k={ALL_K}) and the number of queries whose match sets are identical.",
        "",
        "| pair | queries | errors | top-10 Jaccard | Kendall tau (n) | membership Jaccard | identical match sets |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in compare(runs):
        lines.append(
            f"| {row['pair']} | {row['queries']} | {row['errors']} | {fmt(row['jaccard_top10'])} | "
            f"{fmt(row['tau'])} ({row['tau_n']}) | {fmt(row['member_jaccard'])} | {row['member_identical']}/{row['queries']} |"
        )
    lines += ["", "Match-set size per query (complete match set; `E` = error):", ""]
    lines.append("| query | " + " | ".join(runs) + " |")
    lines.append("|---|" + "---|" * len(runs))
    for name, _ in QUERIES:
        cells = []
        for engine in runs:
            result = runs[engine]["results"][name]
            cells.append("E" if "error" in result else str(len(result["all"])))
        lines.append(f"| `{name}` | " + " | ".join(cells) + " |")
    errors = [(e, n, r["error"]) for e, run in runs.items() for n, r in run["results"].items() if "error" in r]
    if errors:
        lines += ["", "Errors:", "", *[f"- {e} `{n}`: {msg}" for e, n, msg in errors]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workdir", type=Path, default=None, help="sqlite state directory (default: a temporary directory)")
    parser.add_argument("--digests", type=Path, default=None, help="text file of image digests to quote in the report")
    parser.add_argument("--output", type=Path, default=None, help="markdown output (default: stdout)")
    parser.add_argument("--json", type=Path, default=None, help="optional raw per-query results")
    args = parser.parse_args(argv)
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="su_lexical_agreement_"))
    workdir.mkdir(parents=True, exist_ok=True)
    docs, chunks = corpus()
    gen = GenerationId(dataset="demo", generation=secrets.token_hex(16), index_format=3, fingerprint="0" * 64)
    runs: dict[str, dict[str, Any]] = {}
    missing = []
    for kind in ENGINES:
        prefix = f"la{secrets.token_hex(5)}"
        backend = build(kind, workdir, prefix)
        if backend is None:
            missing.append(kind)
            continue
        try:
            runs[kind] = run_engine(kind, backend, gen, docs, chunks)
        finally:
            cleanup(kind, backend)
    text = report(runs, missing, len(docs), len(chunks), args.digests.read_text() if args.digests else None)
    if args.json:
        args.json.write_text(json.dumps(runs, indent=2, sort_keys=True))
    if args.output:
        args.output.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
