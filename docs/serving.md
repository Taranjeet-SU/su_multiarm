# Serving

The same engine is served over HTTP (with server-sent events for streamed answers) and over the
Model Context Protocol (stdio and streamable HTTP). Request bodies and tool arguments use the same
request model as the Python API, and a parity test keeps the surfaces aligned. Ingest first (see the
[Quickstart](quickstart.md)); ingestion itself stays a CLI/Python operation.

Both servers bind to loopback by default. Binding elsewhere requires a bearer token
(`--token-env NAME`); put TLS and per-user controls in front. OAuth2/OIDC, mTLS or per-client keys are
a `PrincipalResolver` you pass in Python, never a class named in configuration.

## HTTP and server-sent events

```bash
pip install -e '.[api]'
su-multi-arm --config examples/v2/local.yaml serve --port 8099 &
sleep 3   # wait for the server to start
curl -s http://127.0.0.1:8099/v2/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"exemption certificate"}'
curl -sN http://127.0.0.1:8099/v2/answer/stream \
  -H 'Content-Type: application/json' \
  -d '{"query":"exemption certificate"}'
```

Interactive OpenAPI documentation is at `/docs`. The stream emits `decision` → `arm_result`… →
`review_round`… → `evidence` → `token`… → `contract` → `done`, or a terminal `error`. `token` events
are provisional fragments sent before validation; only `done` (the full answer, identical to
`/v2/answer`) is authoritative. Tokens are streamed only when the generator role declares
`capabilities.streaming: true`.

## MCP over stdio

```bash
pip install -e '.[mcp]'
```

Register the server with an MCP client:

```json
{
  "mcpServers": {
    "su-multi-arm": {
      "command": "su-multi-arm",
      "args": ["--config", "examples/v2/local.yaml", "mcp"]
    }
  }
}
```

## MCP over streamable HTTP

Enable the transport in the configuration, then start it (path `/mcp`):

```yaml
serving:
  mcp: {transports: [stdio, streamable_http]}
```

```bash
su-multi-arm --config YOUR_CONFIG.yaml mcp --transport streamable_http --port 8100
```

Tools: `search`, `answer` (progress notifications carry the stream events), `capabilities`,
`list_values`, and `get_document` / `aggregate` when enabled under `tools.enabled`. Routes, headers,
error codes and authentication are specified in [Configuration → Serving](CONFIGURATION.md#serving).
