"""``su-multi-arm`` command line.

Every failure prints one concise ``error: ...`` line to stderr (and, for configuration errors, one
indented line per problem with its key path) and exits non-zero: 1 for an operation that failed, 2
for an invalid configuration or command line, 3 when the index is not ready. ``--debug`` re-raises
the exception with its traceback instead (QA-11).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core.config import load_config
from .core.errors import ConfigError, IndexNotReady, SuError
from .engine import Engine
from .models import SearchRequest
from .ingestion.normalize import parse_documents

EXIT_FAILED, EXIT_CONFIG, EXIT_NOT_READY = 1, 2, 3
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def serve_address(bind: str, host: str | None, port: int | None) -> tuple[str, int]:
    """``serve``'s address: the flags when given, else ``serving.http.bind`` (N20)."""
    bound_host, _, bound_port = bind.rpartition(":")
    bound_host = bound_host.strip("[]") or "127.0.0.1"
    return host or bound_host, port if port is not None else int(bound_port or 8099)


def main():
    debug = "--debug" in sys.argv[1:]
    try:
        _main()
    except (SuError, ValueError, OSError) as exc:  # pydantic ValidationError is a ValueError
        if debug:
            raise
        raise SystemExit(_report(exc)) from None


def _report(exc: BaseException) -> int:
    """Print the concise error; return the exit code."""
    from pydantic import ValidationError

    if isinstance(exc, ConfigError):
        problems = [d for d in exc.diagnostics if getattr(d, "severity", "error") == "error"]
        if len(problems) > 1:
            print(f"error: invalid configuration ({len(problems)} problems)", file=sys.stderr)
            for d in problems:
                print(f"  {d.path or '<root>'}: {d.message}", file=sys.stderr)
        else:
            print(f"error: invalid configuration: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if isinstance(exc, ValidationError):
        print(f"error: invalid input ({exc.error_count()} problems)", file=sys.stderr)
        for error in exc.errors():
            print(f"  {'.'.join(str(p) for p in error.get('loc', ())) or '<root>'}: {error.get('msg')}", file=sys.stderr)
        return EXIT_FAILED
    if isinstance(exc, IndexNotReady):
        print(f"error: index not ready: {exc}", file=sys.stderr)
        return EXIT_NOT_READY
    print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
    return EXIT_FAILED


def build_parser() -> argparse.ArgumentParser:
    """The full ``su-multi-arm`` argument parser (also used by tests: UI copy commands must parse)."""
    from . import __version__

    parser = argparse.ArgumentParser(prog="su-multi-arm")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--debug", action="store_true", help="show the Python traceback of an error")
    parser.add_argument("--config", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="validate the config and construct the engine")
    doctor.add_argument(
        "--probe", action="store_true", help="also probe every backend and report provider capability pairs (no model calls)"
    )
    commands.add_parser("status")
    ingest = commands.add_parser("ingest")
    ingest.add_argument("input", type=Path, help="canonical JSONL document file")
    ingest.add_argument("--replace", action="store_true")
    ingest.add_argument(
        "--rebuild", action="store_true", help="allow changed index configuration with --replace"
    )
    ingest.add_argument(
        "--allow-partial", action="store_true", help="activate a generation with incomplete stages (policy: partial)"
    )
    delete = commands.add_parser("delete")
    delete.add_argument("ids", nargs="+")
    for command in ("search", "answer"):
        query = commands.add_parser(command)
        query.add_argument("query", nargs="?")
        query.add_argument(
            "--request", type=Path, help="JSON SearchRequest including context/filters/aggregate"
        )
        query.add_argument("--session", help="session id (see `session create`); turns are recorded")
        query.add_argument("--pipeline", help="named pipeline (must be allowed by request.pipelines)")
        query.add_argument("--mode", choices=["rules", "classifier", "llm", "cascade"], help="decision mode (request.modes)")
    session_cmd = commands.add_parser("session", help="create or close a session in the configured session store")
    session_cmd.add_argument("session_command", choices=["create", "close", "show"])
    session_cmd.add_argument("session_id", nargs="?")
    tool = commands.add_parser("tool", help="call an enabled read-only tool")
    tool.add_argument("name")
    tool.add_argument("--args", default="{}", help="JSON object of tool arguments")
    action = commands.add_parser("action", help="run an allowlisted write action (audited)")
    action.add_argument("name")
    action.add_argument("--args", default="{}", help="JSON object of action arguments")
    action.add_argument("--idempotency-key", required=True)
    action.add_argument("--reason", default="")
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("input", type=Path, help="JSON array of id/request/expected_ids")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", help="bind host (default: serving.http.bind)")
    serve.add_argument("--port", type=int, help="bind port (default: serving.http.bind)")
    serve.add_argument("--token-env", help="bearer token variable (default: serving.http.token_env under auth: bearer_env)")
    mcp_cmd = commands.add_parser("mcp", help="MCP server (stdio or streamable HTTP; serving.mcp.transports)")
    mcp_cmd.add_argument("--transport", choices=["stdio", "streamable_http"], default="stdio")
    mcp_cmd.add_argument("--host", default="127.0.0.1")
    mcp_cmd.add_argument("--port", type=int, default=8100)
    mcp_cmd.add_argument("--token-env", help="bearer token variable for streamable_http (required off localhost)")
    config_cmd = commands.add_parser("config", help="configuration v2 tools: upgrade, validate, schema")
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)
    upgrade = config_sub.add_parser("upgrade", help="write the schema_version 2 equivalent of a v1 config")
    upgrade.add_argument("--output", type=Path, help="destination file (default: print to stdout)")
    upgrade.add_argument("--strict", action="store_true", help="refuse instead of warning when v1 settings cannot be carried")
    config_sub.add_parser("validate", help="validate the config (v1 is upgraded in memory first)")
    schema = config_sub.add_parser("schema", help="print the JSON schema of the v2 (or v1) config model")
    schema.add_argument("--v1", action="store_true")
    sources_cmd = commands.add_parser("sources", help="read-only log sources: check, discover, count, probe (never builds an engine)")
    sources_cmd.add_argument("sources_command", choices=["check", "discover", "count", "probe"])
    sources_cmd.add_argument("--source", help="name under sources: (default: the only one configured)")
    sources_cmd.add_argument("--date", help="partition day YYYY-MM-DD in the source timezone (discover, count, probe)")
    sources_cmd.add_argument("--max-units", type=int, default=3, help="probe: units that get counts and the random_score check")
    sources_cmd.add_argument("--probe-pit", action="store_true", help="probe: also open and close one PIT per sampled unit (only when supported and not disallowed)")
    sources_cmd.add_argument("--no-random-score", action="store_true", help="probe: skip the random_score search check")
    sources_cmd.add_argument("--request-log", type=Path, help="append a content-free JSONL request log (method, path template, row, status, ms, bytes)")
    sources_cmd.add_argument("--max-docs", type=int, default=1000, help="probe: documents pulled from one unit for throughput (0 = none)")
    sources_cmd.add_argument("--output", type=Path, help="write the JSON report to this file instead of stdout")
    patterns_cmd = commands.add_parser("patterns", help="pattern library: lint, coverage, mine, propose, learn, promote, deprecate, disable")
    patterns_sub = patterns_cmd.add_subparsers(dest="patterns_command", required=True)
    patterns_sub.add_parser("lint", help="load, replay and lint extraction.pattern_library")
    for name, text in (
        ("coverage", "line status counts of a log-line file"),
        ("mine", "mine templates and synthesise proposals (nothing is written)"),
        ("propose", "LLM proposals for unparsed clusters, gated, written to proposed.yaml only"),
        ("learn", "one loop: mine, propose, gate, write under the promotion policy, reparse coverage"),
    ):
        sub = patterns_sub.add_parser(name, help=text)
        sub.add_argument("--input", type=Path, required=True, help="one log line per line (UTF-8), or .jsonl with id/text/attributes")
        sub.add_argument("--attr", action="append", default=[], metavar="KEY=VALUE", help="line attribute (service, level, ...)")
        if name in {"coverage", "mine"}:
            sub.add_argument("--source", help="take multiline rules from sources.<name>.multiline (default: the only source, else built-in defaults)")
            sub.add_argument("--no-multiline", action="store_true", help="match every input line as a head (no continuation joining, no fragments)")
        if name in {"propose", "learn"}:
            sub.add_argument("--dry-run", action="store_true", help="stop before any model call: report clusters and estimates; spend and write nothing")
            sub.add_argument("--max-usd", type=float, help="lower the per-run USD cap for this run (needs budgets.price_table)")
            sub.add_argument("--output", type=Path, help="write the JSON report to this file instead of stdout")
    measure = patterns_sub.add_parser("measure-throughput", help="real cluster calls on one route tier over a sealed day's residue: calls/hour, tokens/s (recorded for learning.bootstrap)")
    measure.add_argument("--tier", choices=["local", "lan", "paid"], required=True)
    measure.add_argument("--clusters", type=int, default=50, help="at most this many cluster calls (default 50)")
    measure.add_argument("--date", required=True, help="a sealed partition day YYYY-MM-DD whose residue is used")
    measure.add_argument("--output", type=Path, help="write the JSON report to this file instead of stdout")
    for name, text in (
        ("promote", "approve a proposed (or deprecated) entry: a human decision record"),
        ("deprecate", "stop an approved entry from matching: a human decision record"),
        ("disable", "disable an entry for good (wins over every system record)"),
    ):
        sub = patterns_sub.add_parser(name, help=text)
        sub.add_argument("id")
        sub.add_argument("--reason", default="")
        sub.add_argument("--by", default="cli", help="recorded as user:<by> (letters, digits, _ . @ -)")
    privacy_cmd = commands.add_parser("privacy", help="safe redaction tools: scan files for residual PII shapes")
    privacy_sub = privacy_cmd.add_subparsers(dest="privacy_command", required=True)
    scan = privacy_sub.add_parser("scan", help="content-free leak scan of files, .gz files and SQLite databases (exit 1 on any hit)")
    scan.add_argument("paths", nargs="*", type=Path)
    scan.add_argument("--raw", action="store_true", help="audit the raw-copy store (logs.partitions.raw); without it raw paths are skipped")
    scan.add_argument("--redactors", help="comma-separated built-in redactors (default: the configured safe redactors, else every built-in)")
    scan.add_argument("--max-findings", type=int, default=1000)
    scan.add_argument("--output", type=Path, help="write the JSON report to this file instead of stdout")
    logs_cmd = commands.add_parser("logs", help="daily log partitions and compose (addendum A1 L3a)")
    logs_cmd.add_argument("logs_command", choices=["run-daily", "compose", "backfill", "settle", "reparse", "coverage"])
    logs_cmd.add_argument("--from-raw", action="store_true", help="reparse: re-run redaction and parse from the raw copies (logs.partitions.raw)")
    logs_cmd.add_argument("--date", help="partition day YYYY-MM-DD (default for run-daily: yesterday in logs.timezone)")
    logs_cmd.add_argument("--from", dest="date_from", help="backfill/coverage: first day YYYY-MM-DD")
    retention_cmd = commands.add_parser("retention", help="retention of log partitions and generations (addendum A1 L3b)")
    retention_cmd.add_argument("retention_command", choices=["report", "verify", "run"])
    retention_cmd.add_argument("--date", help="as-of day YYYY-MM-DD (default: today in logs.timezone)")
    return parser


def _main():
    parser = build_parser()
    args = parser.parse_args()
    if args.command in {"logs", "retention"}:
        from .ingestion.schedule import logs_cli

        result = logs_cli(args, load_config(args.config))
        print(json.dumps(result, indent=2, default=str))
        if args.command == "logs" and args.logs_command == "coverage" and not result.get("ok", True):
            raise SystemExit(1)  # an open (unsealed) day or an accounting violation
        return
    if args.command == "config":
        print(_config_command(args))
        return
    if args.command == "privacy":
        report = _privacy_scan(args)
        text = json.dumps(report, indent=2)
        if args.output:
            args.output.write_text(text + "\n")
            print(json.dumps({"written": str(args.output), "hits": report["hits"]}))
        else:
            print(text)
        if report["hits"]:
            raise SystemExit(EXIT_FAILED)
        return
    if args.command == "sources":
        _sources_command(args, parser)
        return
    if args.command == "patterns":
        output = _patterns_command(args)
        text = json.dumps(output, indent=2, ensure_ascii=False, default=str)
        if getattr(args, "output", None):
            args.output.write_text(text + "\n")
            print(json.dumps({"written": str(args.output), "stopped": output.get("stopped")}))
        else:
            print(text)
        if output.get("stopped"):
            raise SystemExit(EXIT_FAILED)
        return
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        config = load_config(args.config)
    try:
        if args.command == "doctor":
            check_ui_theme(config)
        engine = Engine(config)
    except ConfigError as exc:
        if args.command != "doctor":
            raise
        print(json.dumps({"config": "invalid", "error": str(exc), "path": exc.path, "warnings": [str(w.message) for w in caught]}, indent=2))
        raise SystemExit(1)
    if args.command == "doctor":
        output = {
            "config": "valid",
            "schema_version": config.schema_version,
            "dataset": config.dataset,
            "profile": config.profile,
            "backends": {name: backend.kind for name, backend in config.backends.items()},
            "decision": config.pipeline.decision.kind,
            "enabled_arms": sorted(config.pipeline.arms.enabled()),
            "extraction_policy": config.extraction.policy,
            "warnings": [str(w.message) for w in caught],
            "warning": "hash embeddings are lexical demo vectors, not semantic embeddings"
            if config.models.roles.embedder.provider == "hash"
            else None,
        }
        output["pipelines"] = sorted(engine.pipelines)
        output["default_pipeline"] = config.default_pipeline
        # Unset API-key variables per pipeline and for ingest. Keys only a non-default pipeline or
        # mode needs are checked on first use, so they do not stop construction (QA-14).
        output["missing_keys"] = engine.missing_keys()
        if config.serving.ui.enabled:
            from importlib.util import find_spec

            output["ui"] = {"mount": config.serving.ui.mount, "bundle_installed": find_spec("su_multi_arm_ui") is not None}
            if config.serving.ui.allow_unauthenticated_loopback and config.serving.http.auth != "bearer_env":
                output["ui"]["warning"] = "serving.ui.allow_unauthenticated_loopback: any local user or web page reaching the port can query the data"
            bind_host = config.serving.http.bind.rpartition(":")[0].strip("[]")
            if bind_host not in LOOPBACK:
                # The server speaks plain HTTP: off loopback the bearer token crosses the network in clear.
                output["ui"]["tls_warning"] = (
                    f"serving.http.bind {config.serving.http.bind} is not loopback and the server has no TLS; "
                    "put a TLS-terminating forwarder in front and name it in serving.allowed_hosts"
                )
        try:
            manifest = engine.manifest()
            output["active_generation"] = manifest["generation"]
            output["generation_status"] = manifest["status"]
        except ValueError as exc:
            output["index"] = str(exc)
        if args.probe:
            from .pipeline.validate import probe_report

            output["probe"] = probe_report(engine)
            if output["probe"]["probe"] != "ok":
                print(json.dumps(output, indent=2, ensure_ascii=False))
                raise SystemExit(1)
    elif args.command == "ingest":
        text = args.input.read_text()
        records = (
            json.loads(text)
            if text.lstrip().startswith("[")
            else [json.loads(line) for line in text.splitlines() if line.strip()]
        )
        documents = parse_documents(records, config)
        output = engine.ingest(documents, replace=args.replace, rebuild=args.rebuild, allow_partial=args.allow_partial)
    elif args.command == "delete":
        output = engine.ingest([], delete_ids=args.ids)
    elif args.command == "status":
        output = engine.status()
    elif args.command in {"search", "answer"}:
        request = (
            SearchRequest.model_validate_json(args.request.read_text())
            if args.request
            else SearchRequest(query=args.query or "")
        )
        overrides = {k: v for k, v in {"pipeline": args.pipeline, "mode": args.mode}.items() if v is not None}
        if overrides:
            request = SearchRequest.model_validate({**request.model_dump(), **overrides})
        output = getattr(engine, args.command)(request, session_id=args.session).model_dump(mode="json")
    elif args.command == "session":
        if args.session_command == "create":
            output = {"session_id": engine.create_session(session_id=args.session_id)}
        elif not args.session_id:
            parser.error(f"session {args.session_command} needs a session id")
        elif args.session_command == "close":
            engine.close_session(args.session_id)
            output = {"closed": args.session_id}
        else:
            output = engine.session(args.session_id).record.model_dump(mode="json")
    elif args.command == "tool":
        output = engine.call_tool(args.name, json.loads(args.args)).model_dump(mode="json")
    elif args.command == "action":
        output = engine.run_action(
            args.name, json.loads(args.args), idempotency_key=args.idempotency_key, reason=args.reason, surface="cli"
        ).model_dump(mode="json")
    elif args.command == "evaluate":
        from .evaluation import evaluate

        output = evaluate(engine, json.loads(args.input.read_text()))
    elif args.command == "serve":
        args.host, args.port, args.token_env = serve_binding(config, args.host, args.port, args.token_env)
        host, port = args.host, args.port
        if host not in LOOPBACK and not args.token_env:
            parser.error("network binding requires --token-env or serving.http.auth: bearer_env; use a TLS reverse proxy for remote access")
        import uvicorn
        from .api import create_app

        uvicorn.run(create_app(engine=engine, token_env=args.token_env, bind=f"{host}:{port}"), host=host, port=port)
        return
    else:
        from .serving.mcp import create_mcp, mcp_http_app, run_stdio

        if args.transport == "stdio":
            run_stdio(create_mcp(engine=engine))
            return
        if args.host not in {"127.0.0.1", "localhost", "::1"} and not (args.token_env or config.serving.http.auth == "bearer_env"):
            parser.error("network binding requires --token-env; use a TLS reverse proxy for remote access")
        import uvicorn

        server = create_mcp(engine=engine, host=args.host, port=args.port)
        uvicorn.run(mcp_http_app(server, args.token_env, bind=f"{args.host}:{args.port}"), host=args.host, port=args.port)
        return
    print(json.dumps(output, indent=2, ensure_ascii=False))


def serve_binding(config, host: str | None, port: int | None, token_env: str | None) -> tuple[str, int, str | None]:
    """``serve`` binding: command-line values win, else ``serving.http.bind`` (``host:port``, IPv6
    as ``[::1]:port``) and, under ``auth: bearer_env``, ``serving.http.token_env``."""
    http = config.serving.http
    bound_host, _, bound_port = http.bind.rpartition(":")
    bound_host = bound_host.strip("[]") or "127.0.0.1"
    if token_env is None and http.auth == "bearer_env":
        token_env = http.token_env
    return host or bound_host, port if port is not None else int(bound_port), token_env


def _sources_command(args, parser) -> None:
    """``sources check|discover|count|probe``. Loads the config, constructs the named source and
    talks to it only through its allow-list transport. Output is aggregate JSON; credentials come
    from the ``*_env`` variables and are never printed."""
    from .sources import build_source
    from .sources.cursor import install_signal_handlers
    from .sources.probe import day_window, run_probe

    config = load_config(args.config)
    if not config.sources:
        raise ConfigError("no sources are configured", "sources")
    name = args.source or (next(iter(config.sources)) if len(config.sources) == 1 else None)
    if name is None:
        parser.error(f"several sources are configured; pass --source ({sorted(config.sources)})")
    if name not in config.sources:
        raise ConfigError(f"unknown source {name!r}; configured: {sorted(config.sources)}", "sources")
    if args.sources_command != "check" and not args.date:
        parser.error(f"sources {args.sources_command} needs --date YYYY-MM-DD")
    install_signal_handlers()
    source = build_source(name, config.sources[name])
    failed = False
    try:
        if args.sources_command == "check":
            caps = source.capabilities(probe=True)
            diagnostics = source.self_check()
            failed = any(d.severity == "error" for d in diagnostics)
            output = {"source": name, "capabilities": caps.model_dump(), "self_check": [d.model_dump() for d in diagnostics]}
        elif args.sources_command == "probe":
            output = run_probe(source, args.date, max_units=args.max_units, max_docs=args.max_docs, probe_pit=args.probe_pit, random_score=not args.no_random_score)
            failed = any(d["severity"] == "error" for d in output["self_check"])
        else:
            units = source.discover(day_window(args.date, source.config.timezone))
            if args.sources_command == "discover":
                output = {"source": name, "units": [u.model_dump(mode="json") for u in units]}
            else:
                output = {"source": name, "counts": [{"index": u.index, **source.count(u).model_dump(mode="json")} for u in units]}
            output["diagnostics"] = [d.model_dump() for d in source.diagnostics]
            output["excluded_scope"] = dict(source.excluded_scope)
    finally:
        if args.request_log is not None and source._transport is not None:
            with args.request_log.open("a", encoding="utf-8") as handle:
                rows = source._transport.request_log()
                for row in rows:
                    handle.write(json.dumps({"command": args.sources_command, **row}, sort_keys=True) + "\n")
                # the transport keeps the last 2000 records; the summary row says if any were dropped
                handle.write(json.dumps({"command": args.sources_command, "summary": True, "sent": source._transport.sent, "logged": len(rows)}, sort_keys=True) + "\n")
        source.shutdown()
    text = json.dumps(output, indent=2, ensure_ascii=False, default=str)
    if args.output:
        args.output.write_text(text + "\n")
        print(json.dumps({"written": str(args.output)}))
    else:
        print(text)
    if failed:
        raise SystemExit(EXIT_FAILED)


def _privacy_scan(args) -> dict:
    """``privacy scan``: the configured safe redactors (or ``--redactors``, or every built-in) over
    the given paths. Never prints matched text."""
    from .core.config import REDACTOR_NAMES
    from .extraction.patterns.engine import select_engine
    from .privacy import SafeRedactor, builtin_redactors, safe_redactor, scan_paths

    config = load_config(args.config)
    if args.redactors:
        names = [n.strip() for n in args.redactors.split(",") if n.strip()]
        privacy = config.privacy
        redactor = SafeRedactor(builtin_redactors(names), select_engine(privacy.regex_engine, timeout_ms=privacy.regex_timeout_ms))
    else:
        redactor = safe_redactor(config.privacy)
        if redactor is None:
            redactor = SafeRedactor(builtin_redactors(list(REDACTOR_NAMES)), select_engine(config.privacy.regex_engine, timeout_ms=config.privacy.regex_timeout_ms))
    raw = config.logs.partitions.raw if config.logs is not None else None
    raw_root = raw.path if raw is not None and raw.enabled else None
    paths = list(args.paths)
    if args.raw:
        if raw_root is None:
            raise ConfigError("privacy scan --raw needs logs.partitions.raw enabled", "logs.partitions.raw")
        from .ingestion.raw_reparse import audit_root, open_raw

        paths.append(audit_root(open_raw(config), purpose="audit"))
    if not paths:
        raise ConfigError("privacy scan needs at least one path (or --raw)", "privacy")
    exclude = [] if args.raw or raw_root is None else [raw_root]
    report = scan_paths(paths, redactor, max_findings=args.max_findings, exclude=exclude).to_dict()
    return {"redactors": redactor.names, "regex_engine": redactor.engine.kind, "raw": bool(args.raw), **report}


def _input_lines(args) -> list:
    """``--input``: plain lines (ids ``l<n>``, ``--attr`` attributes) or JSONL objects with ``text``
    and optional ``id``, ``attributes`` and ``ts`` (ISO 8601 with offset; used by multiline joining)."""
    return [line for line, _ in _input_rows(args)]


def _input_rows(args) -> list:
    from datetime import datetime

    from .learning.patterns import LogLine

    attributes = {}
    for item in args.attr:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"--attr expects KEY=VALUE, got {item!r}")
        attributes[key] = value
    raw = args.input.read_text(encoding="utf-8").splitlines()
    if args.input.suffix == ".jsonl":
        rows = []
        for number, row in enumerate(raw):
            if not row.strip():
                continue
            data = json.loads(row)
            if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                raise ValueError(f"{args.input}:{number + 1}: each JSONL row needs a string 'text'")
            attrs = {**attributes, **{str(k): str(v) for k, v in (data.get("attributes") or {}).items()}}
            stamp = None
            if data.get("ts") is not None:
                try:
                    stamp = datetime.fromisoformat(str(data["ts"]).replace("Z", "+00:00"))
                except ValueError:
                    raise ValueError(f"{args.input}:{number + 1}: 'ts' is not an ISO 8601 timestamp") from None
                if stamp.tzinfo is None:
                    raise ValueError(f"{args.input}:{number + 1}: 'ts' needs a UTC offset")
            rows.append((LogLine(str(data.get("id") or f"l{number}"), data["text"], attrs), stamp))
        return rows
    return [(LogLine(f"l{i}", line, attributes), None) for i, line in enumerate(row for row in raw if row.strip())]


def _patterns_decision(args, config, ref) -> dict:
    """``patterns promote|deprecate|disable``: one human record in decisions.yaml (CLI only)."""
    from .extraction.patterns.engine import select_engine
    from .extraction.patterns.library import load_library
    from .extraction.patterns.writer import commit_system_files, git_work_tree, require_clean_system_files
    from .learning.patterns import human_decision

    library = load_library(ref.path, select_engine(ref.engine, timeout_ms=ref.regex_timeout_ms), min_literal_chars=ref.min_literal_chars, strict=False)
    settings = config.learning.patterns
    commit = settings is not None and settings.git_commit and git_work_tree(ref.path)
    if commit:
        require_clean_system_files(ref.path)
    decision = human_decision(ref.path, args.patterns_command, args.id, by=f"user:{args.by}", library=library, reason=args.reason)
    sha = commit_system_files(ref.path, "system:cli", f"patterns {args.patterns_command} {args.id}") if commit else None
    return {"decision": decision.model_dump(mode="json", exclude_none=True), "git_commit": sha}


def _patterns_command(args) -> dict:
    """``patterns lint|coverage|mine`` read the library and an input file and write nothing.
    ``propose|learn`` run the learning loop (``learning/patterns/loop.py``); ``promote|deprecate|
    disable`` append one human decision record. None of them constructs an engine or touches an
    index; none is an ``Action`` (no HTTP/MCP surface)."""
    from .extraction.patterns.coverage import coverage
    from .extraction.patterns.extractor import PatternExtractor

    config = load_config(args.config)
    ref = config.extraction.pattern_library
    if ref is None:
        raise ConfigError("extraction.pattern_library is not configured", "extraction.pattern_library")
    if args.patterns_command in {"promote", "deprecate", "disable"}:
        return _patterns_decision(args, config, ref)
    if args.patterns_command == "measure-throughput":
        return measure_throughput(config, tier=args.tier, clusters=args.clusters, day=args.date)
    if args.patterns_command in {"propose", "learn"}:
        from .learning.patterns import session_from_config

        session = session_from_config(config, dry_run=args.dry_run, max_usd=args.max_usd)
        output = getattr(args, "output", None)

        def progress(partial: dict) -> None:  # a stopped run keeps its report
            if output is not None:
                output.write_text(json.dumps(partial, indent=2, ensure_ascii=False, default=str) + "\n")

        return session.run(_input_lines(args), mode=args.patterns_command, dry_run=args.dry_run, progress=progress).to_dict()
    from .extraction.predicate_support import PredicateSupport

    if args.patterns_command == "lint":
        return _patterns_lint(ref)
    compiled = PatternExtractor(ref, PredicateSupport(config.extraction.stemmer_language)).load()
    lines, fragments, multiline = _assembled_lines(args, config, compiled)
    if args.patterns_command == "coverage":
        return {**coverage((compiled.match_line(line.text, attributes=line.attributes) for line in lines), fragments).to_dict(), "multiline": multiline}
    from .learning.patterns import bootstrap

    report = bootstrap(
        lines,
        compiled,
        min_support=ref.gate.min_support,
        min_literal_chars=ref.min_literal_chars,
        fragments=fragments,
    )
    return {**report.to_dict(), "multiline": multiline, "proposals": [p.model_dump(mode="json", exclude_none=True) for p in report.proposals]}


def measure_throughput(config, *, tier: str, clusters: int, day: str, transport=None, probe=None) -> dict:
    """``patterns measure-throughput``: real calls on the routes of one tier (under every cap and the
    egress policy), recorded in ``throughput.json`` for ``learning.bootstrap``. ``transport`` and
    ``probe`` are for tests and the Python API only."""
    from .extraction.patterns.extractor import PatternExtractor
    from .ingestion.partitions import FilePartitionStore
    from .learning.extraction import FallbackSession
    from .learning.extraction.throughput import measure

    if config.logs is None:
        raise ConfigError("measure-throughput reads a sealed log partition and needs logs:", "logs")
    session = FallbackSession.from_config(config, transport=transport, probe=probe)
    store = FilePartitionStore(config.logs.partitions.path, config.logs.source)
    library = PatternExtractor(config.extraction.pattern_library, None).load()
    return measure(session, store, day, library, tier=tier, clusters=clusters)


def _patterns_lint(ref) -> dict:
    """Every lint issue, as structured rows and rendered lines. A library with errors is reported in
    full (never truncated) and the command exits non-zero (``stopped``)."""
    from .extraction.patterns.engine import select_engine
    from .extraction.patterns.library import load_library

    engine = select_engine(ref.engine, timeout_ms=ref.regex_timeout_ms)
    library = load_library(ref.path, engine, min_literal_chars=ref.min_literal_chars, strict=False)
    errors = library.errors()
    return {
        "library": library.name,
        "provenance": {"library_identity": library.identity(), **engine.identity(), "engine_deterministic": engine.deterministic},
        "approved": [e.entry.id for e in library.approved()],
        "statuses": {pid: e.status for pid, e in sorted(library.entries.items())},
        "errors": len(errors),
        "warnings": sum(1 for i in library.issues if i.severity == "warning"),
        "issues": [i.render() for i in library.issues],
        "issue_rows": [{"severity": i.severity, "code": i.code, "pattern_id": i.pattern_id, "source": i.source, "message": i.message} for i in library.issues],
        "stopped": f"{len(errors)} lint error(s)" if errors else None,
    }


def _assembled_lines(args, config, compiled) -> tuple[list, int, dict]:
    """The input as multiline **heads**, the way ingest sees it: each input line (or JSONL row) is
    one log document, in input order and one stream per ``container``/``file`` attribute (a JSONL
    ``ts`` orders and gaps the rows; without one the rows are contiguous). Continuation lines
    (built-in rules of the source's ``multiline`` config plus the library's approved
    ``continuation`` entries) join the preceding head; head-less ones are ``fragment``s. A JSONL
    ``text`` with embedded newlines is one record whose first line is the head. Returns the heads,
    the fragment count and the assembly statistics."""
    from datetime import datetime, timezone

    from .core.config_sources import MultilineConfig
    from .learning.patterns import LogLine
    from .sources.logs import LogRecord
    from .sources.multiline import assemble

    rows = _input_rows(args)
    lines = [line for line, _ in rows]
    stamps = {line.id: stamp for line, stamp in rows}
    if len(stamps) != len(lines):
        raise ValueError(f"{args.input}: line ids must be unique")
    if args.no_multiline:
        return [LogLine(line.id, line.text.split("\n", 1)[0], line.attributes) for line in lines], 0, {"enabled": False}
    sources = config.sources or {}
    if args.source is not None:
        if args.source not in sources:
            raise ConfigError(f"unknown source {args.source!r}; configured: {sorted(sources)}", "sources")
        multiline = sources[args.source].multiline
    elif len(sources) == 1:
        multiline = next(iter(sources.values())).multiline
    else:
        multiline = MultilineConfig()
    base = datetime(1970, 1, 1, tzinfo=timezone.utc)
    records = []
    for number, line in enumerate(lines):
        attrs = dict(line.attributes)
        stamp = stamps.get(line.id)
        head, _, rest = line.text.partition("\n")
        records.append(
            LogRecord(
                id=line.id,
                ts=stamp or base,
                partition_key="cli",
                attributes={k: v for k, v in attrs.items() if k not in {"container", "file"}},
                level=attrs.get("level"),
                message=head,
                stack=rest or None,
                container=attrs.get("container"),
                file=attrs.get("file"),
                offset=number,
                source_id=line.id,
                index="cli",
            )
        )
    heads, stats = assemble(records, multiline, library=compiled)
    by_id = {line.id: line for line in lines}
    order = {line.id: n for n, line in enumerate(lines)}
    out = [LogLine(r.id, r.message, by_id[r.id].attributes) for r in sorted(heads, key=lambda r: order[r.id])]
    return out, stats.fragments, {"enabled": True, "rules": list(multiline.continuation), **stats.as_dict()}


def check_ui_theme(config) -> None:
    """``serving.ui.theme`` (resolved relative to the config file) must exist and be a valid theme:
    the same check ``serve`` makes at start-up, so ``config validate`` and ``doctor`` catch it first
    (``ConfigError`` naming the problem)."""
    if config.serving.ui.theme is None:
        return
    from .serving.ui_theme import load_theme

    load_theme(config.serving.ui.theme)


def _config_command(args) -> str:
    """``config upgrade|validate|schema``; never constructs an engine or touches an index."""
    import warnings

    from .core import schema as schema_module
    from .core.config import load_config as load_config_v2

    if args.config_command == "upgrade":
        from .compat.config_v1 import upgrade_text

        if args.output:
            # Paths in the file are relative to the file: rebase them for the output's directory.
            text, _ = upgrade_text(
                args.config.read_text(), strict=args.strict, source_dir=args.config.resolve().parent, target_dir=args.output.resolve().parent
            )
            args.output.write_text(text)
            return json.dumps({"written": str(args.output)})
        text, _ = upgrade_text(args.config.read_text(), strict=args.strict)
        return text.rstrip("\n")
    if args.config_command == "schema":
        if args.v1:
            from .compat.config_v1 import Config as ConfigV1

            return schema_module.render(ConfigV1).rstrip("\n")
        return schema_module.render().rstrip("\n")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        config = load_config_v2(args.config)
    check_ui_theme(config)
    return json.dumps(
        {
            "config": "valid",
            "schema_version": config.schema_version,
            "dataset": config.dataset,
            "profile": config.profile,
            "backends": {name: backend.kind for name, backend in config.backends.items()},
            "arms": sorted(config.pipeline.arms.enabled()),
            "decision": config.pipeline.decision.kind,
            "fingerprint": config.fingerprint(),
            "warnings": [str(w.message) for w in caught],
        },
        indent=2,
    )


if __name__ == "__main__":
    main()
