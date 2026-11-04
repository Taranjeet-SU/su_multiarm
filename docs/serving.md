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

## Web UI

A read-only browser UI (preview) ships as a separate wheel, `su-multi-arm-ui` (package
`su_multi_arm_ui`: static HTML/JS/CSS, a `manifest.json` with the sha256 of every file and
`THIRD_PARTY_NOTICES.txt`). The core wheel and sdist never contain it and never need Node. It is a
Vite + React + TypeScript build; a static Next.js export would also need no Node server at run time,
and the choice rests on packaging and simplicity: no SSR or routing features are needed, one small
toolchain, and same-origin static files give one port and one auth.

```bash
pip install ./ui/python            # from a checkout (the built bundle is committed); or 'su-multi-arm[ui]'
```

```yaml
serving:
  http: {bind: 127.0.0.1:8099, auth: bearer_env, token_env: SU_UI_TOKEN}
  ui:
    enabled: true                    # without the wheel, serve refuses to start (ConfigError)
    mount: /ui
    session_storage_token: false     # true: the token survives a reload in this tab (sessionStorage)
    allow_unauthenticated_loopback: false
  allowed_hosts: []                  # extra Host names (a reverse proxy's name), exact match
```

`su-multi-arm --config YOUR_CONFIG.yaml serve` then serves the UI at `http://127.0.0.1:8099/ui/`.
`serve` takes its host and port from `serving.http.bind` unless `--host`/`--port` are given, and its
non-loopback guard accepts `--token-env` or `serving.http.auth: bearer_env`.

**Views.** Ask (the answer streamed over `POST /v2/answer/stream`, provisional tokens shown dimmed
until the authoritative `done` result, citation chips that jump to the evidence, the contract badge
`passed`/`stripped`/`abstained` with its violations, unsupported constraints as chips); evidence tabs
per arm (fused, dense, lexical with matched terms highlighted, graph, communities, any other arm the
server returns, structured aggregates with the exactness badge `= exact`/`≤ bounded`/`≈ approximate`,
basis, denominator, total, missing values and the truncated-ids flag); a graph explorer; charts of
grouped aggregates (`SearchRequest.aggregate: group_count`; date fields as a line); a trace panel
(decision, arms fired per round, timings, model calls with tokens and USD; calls without a reported
price are counted, never summed as zero); settings (token, capabilities, advertised routes and tools).

**Graph explorer.** This version has no `/v2/graph/neighbourhood` route, so the explorer runs a search
for the entity and draws only what the graph arm returned: stored edges (`subject -predicate->
object` with their quote) and entity-to-document mentions (`mentioned_in`, dashed). Two entities in
the same passage are never connected unless the server returned an edge. When the server advertises
the neighbourhood route (addendum A2 §4.3) the UI uses it instead; the page says which source it
shows.

**Security model.**

- Static assets under the mount are public (the page has to load before it can ask for the token)
  and contain no data; every `/v2/*` call goes through the `PrincipalResolver`. The token is kept in
  memory (sessionStorage only when `session_storage_token`), never in `localStorage` or a cookie, so
  the API stays CSRF-immune.
- `ui.enabled` requires `auth: bearer_env` or a resolver injected in Python;
  `allow_unauthenticated_loopback: true` is an explicit single-user opt-out that `doctor` warns about.
- With the UI enabled, a Host allow-list (`127.0.0.1`, `localhost`, `[::1]`, the bind host,
  `serving.allowed_hosts`) answers any other Host with 421 (DNS rebinding), and a foreign `Origin` on
  a non-GET route or on `/v2/answer/stream` gets 403. `X-Content-Type-Options: nosniff` is set on every
  `/v2/*` response.
- `/ui/*` responses carry `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src
  'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none';
  frame-ancestors 'none'; base-uri 'none'; form-action 'none'`, plus `nosniff`, `Referrer-Policy:
  no-referrer` and `X-Frame-Options: DENY`; `index.html` is `no-store`, hashed assets are immutable.
- Every log line, title, quote and model reply is rendered as a text node (no raw HTML, no markdown
  rendering); ANSI escapes and control characters are removed and bidi overrides are shown as
  `[U+202E]`; only absolute `http(s)` URLs become links (`rel="noopener noreferrer"`, URL visible).
- No fallback data: an API error is shown as a typed banner (401 token prompt, 403, 404, 409, 422,
  429, 499, 502, 503, 504); optional routes and tools are used only when `/openapi.json` or
  `GET /v2/tools` advertises them.

**Building the bundle** (only to change the UI; the built bundle is committed):

```bash
cd ui && npm ci && npm run build && npx vitest run   # Node 24 (.nvmrc); writes python/su_multi_arm_ui/static
python -m build ui/python                             # fails if the bundle is missing or empty
```

**Browser smoke** against a synthetic logs engine (fake streaming model, no network):

```bash
PYTHONPATH=src:tests python ui/e2e/serve_synthetic.py --workdir /tmp/ui-smoke --port 18099 &
node ui/e2e/smoke.mjs http://127.0.0.1:18099/ui/ smoke-token /tmp/ui-shots   # needs a Playwright chromium
```

### Inspect routes and the pattern review view

Read-only routes for the UI and operators (scope `inspect`, held by the built-in principals;
single-tenant: with an access policy they answer 403). Absent data answers `available: false` with a
reason, never zeros.

| Route | What |
|---|---|
| `GET /v2/info` | the active generation: dataset, logs window, date basis, timezone, partitions, pattern provenance, counts |
| `GET /v2/runs?limit=` (≤ 31) | content-free daily run reports, newest first |
| `GET /v2/spend?days=` (≤ 31) | the pattern learner's per-day spend ledger, read-only |
| `GET /v2/patterns/view` | the library snapshot the generation was built with |
| `GET /v2/patterns/review?kind=` | the review queue: proposals with redacted examples, matched-line count and gate checks, learned entries, decision history (read from the library's working files, never written) |
| `POST /v2/graph/neighbourhood` | `{query or entity_ids, hops ≤ 3, edge_kinds, predicates, max_nodes ≤ 300, max_edges ≤ 1000}` → stored nodes and edges, `truncated` when a bound applied |
| `GET /v2/graph/edges/{id}` | an edge's stored spans (≤ 20) |
| `GET /v2/communities?limit=`, `GET /v2/communities/{id}` | the generation's communities |

The **Patterns** view lists the proposals (default: kind `noise`, status `proposed`) with their
regex, redacted example lines, matched lines and gate checks. Each card builds a decision command
for `promote`, `deprecate` or `disable`, quoted with POSIX `shlex.quote` rules:

```text
su-multi-arm --config=config.yaml patterns deprecate --by=alice '--reason=noisy since the release' -- noise.metrics_flush
```

The UI never writes. Run the command where the library lives; the decision takes effect at the next
compose. Over plain HTTP on a LAN name the browser has no clipboard API; the command is then shown to
copy by hand. With the UI behind a LAN forwarder, put the forwarder's host name in
`serving.allowed_hosts` (the Host match ignores the port).

### Theming the UI

`serving.ui.theme` names a JSON theme file; without it the neutral default is used. The server
validates the file at start-up (an invalid theme refuses the start) and serves it as
`/ui/theme.css` (CSS custom properties only) and `/ui/theme-logo.svg`, so the strict CSP still
holds. Keep your branding in the deployment's configuration directory, not in this repository.

```json
{
  "name": "example-brand",
  "product_name": "Example Log Desk",
  "tagline": "Answers from your platform logs",
  "colors": {
    "brand": "#c2410c", "brand_ink": "#ffffff", "brand_soft": "#ffedd5", "brand_tint": "#fff7ed",
    "ink": "#1f2937", "muted": "#6b7280", "surface": "#f8fafc", "card": "#ffffff", "border": "#e5e7eb"
  },
  "fonts": {"sans": "\"Source Sans 3\", system-ui, sans-serif", "mono": "ui-monospace, monospace"},
  "series": ["#1d4ed8", "#b45309", "#166534", "#7c3aed", "#0891b2", "#be123c"],
  "logo_svg_file": "logo.svg"
}
```

- Colours are `#rrggbb`. A very light brand colour (an orange, for example) reads well on buttons
  and accents but is too pale for chart marks: charts use `series`, never `brand`. Check a custom
  `series` list with a palette validator for contrast on the card colour.
- Fonts must be installed on the viewer's machine: the CSP allows no remote font. A stack holds
  family names, quotes, commas, spaces and hyphens only.
- `logo_svg_file` is relative to the theme file and must stay in its directory: at most 200 kB, an
  `<svg>` root, no script, event attributes, `foreignObject`, entities or external references. It is
  shown through an `<img>` tag.
- `product_name` and `tagline` are plain text.

```yaml
serving:
  ui: {enabled: true, theme: /etc/su-multi-arm/ui-theme.json}
```
