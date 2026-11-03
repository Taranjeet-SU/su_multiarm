# Contributing

Start with the research, architecture and implementation status under `docs/`. Install `.[dev,api,mcp,faiss,communities]`,
run `pytest -q`, then `ruff check src tests`. Use synthetic examples only. Do not commit customer
records, question sets, model weights, indexes, tokens, private endpoints or `.env` files.

For a backend, add an optional dependency, typed configuration, a lazy adapter and integration tests
that cover filtering before top-k, canonical IDs, update/delete and failed-build behavior. State
whether validation used mocks or a real service. Relevance changes need per-arm and union metrics
on frozen labels, including regressions; never report a synthetic smoke test as customer accuracy.

Keep default installation usable offline. Do not introduce hidden retries, model fallback, paid
requests, automatic telemetry or API-key discovery. Keep documentation and generated config schema
in step with code. Run `python -m build` and verify a clean wheel install before release.

Pull requests should explain the user-visible change, tradeoff, validation and limits. Contributions
are submitted under Apache-2.0. Maintainers still need to choose a public security contact and release
process before external publication.
