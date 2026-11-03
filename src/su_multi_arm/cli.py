from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core.config import load_config
from .core.errors import ConfigError
from .engine import Engine
from .models import SearchRequest
from .ingestion.normalize import parse_documents


def main():
    from . import __version__

    parser = argparse.ArgumentParser(prog="su-multi-arm")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8099)
    serve.add_argument("--token-env")
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
    args = parser.parse_args()
    if args.command == "config":
        print(_config_command(args))
        return
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        config = load_config(args.config)
    try:
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
        if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.token_env:
            parser.error("network binding requires --token-env; use a TLS reverse proxy for remote access")
        import uvicorn
        from .api import create_app

        uvicorn.run(create_app(engine=engine, token_env=args.token_env), host=args.host, port=args.port)
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
        uvicorn.run(mcp_http_app(server, args.token_env), host=args.host, port=args.port)
        return
    print(json.dumps(output, indent=2, ensure_ascii=False))


def _config_command(args) -> str:
    """``config upgrade|validate|schema``; never constructs an engine or touches an index."""
    import warnings

    from .core import schema as schema_module
    from .core.config import load_config as load_config_v2

    if args.config_command == "upgrade":
        from .compat.config_v1 import upgrade_text

        text, _ = upgrade_text(args.config.read_text(), strict=args.strict)
        if args.output:
            args.output.write_text(text)
            return json.dumps({"written": str(args.output)})
        return text.rstrip("\n")
    if args.config_command == "schema":
        if args.v1:
            from .compat.config_v1 import Config as ConfigV1

            return schema_module.render(ConfigV1).rstrip("\n")
        return schema_module.render().rstrip("\n")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        config = load_config_v2(args.config)
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
