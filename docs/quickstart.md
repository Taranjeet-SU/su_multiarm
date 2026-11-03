# Quickstart

Runs fully offline: a local SQLite backend, hash embeddings (a smoke-test device, not a semantic
model) and no chat model. Python 3.11+ on Linux or macOS (advisory file locking uses `fcntl`).

## Install from a clone

```bash
git clone https://github.com/Taranjeet-SU/su_multiarm.git
cd su_multiarm
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

Once the package is published, `pip install "su-multi-arm[all]"` will install it with every optional
backend and provider; until then install from a clone.

## CLI

Ingest the synthetic example corpus, then search, ask for a typed aggregate, answer and evaluate:

```bash
su-multi-arm --config examples/v2/local.yaml ingest examples/documents.jsonl
su-multi-arm --config examples/v2/local.yaml search 'exemption certificate'
su-multi-arm --config examples/v2/local.yaml search --request examples/aggregate.json
su-multi-arm --config examples/v2/local.yaml answer 'exemption certificate'
su-multi-arm --config examples/v2/local.yaml evaluate examples/evaluation.json
su-multi-arm --config examples/v2/local.yaml doctor
```

## Python

```python
import json

from su_multi_arm import Engine, SearchRequest, load_config
from su_multi_arm.ingestion.normalize import parse_documents

config = load_config("examples/v2/local.yaml")
engine = Engine(config)

with open("examples/documents.jsonl") as handle:
    records = [json.loads(line) for line in handle if line.strip()]
print(engine.ingest(parse_documents(records, config))["status"])  # complete

result = engine.search(SearchRequest(query="exemption certificate"))
for hit in result.hits[:3]:
    print(hit.document_id, hit.arms)

answer = engine.answer(SearchRequest(query="exemption certificate"))
print(answer.contract, answer.answer.answer)  # generation is disabled until a model is configured
```

Every ingest stages a new immutable generation and switches to it only after every projection
validates; a failed build leaves the previous generation active. Each hit carries the arms that found
it and the field offsets of its passages. With no chat model configured, `answer` returns the
retrieved evidence and says that generation is disabled.

## Operations

| Operation | CLI | Python | What it guarantees |
|---|---|---|---|
| Ingest | `ingest FILE [--replace --rebuild]` | `engine.ingest(docs)` | New generation activated only after every projection validates |
| Delete | `delete ID...` | `engine.ingest([], delete_ids=[...])` | Derived facts and memberships rebuilt in a new generation |
| Search | `search QUERY` / `--request FILE` | `engine.search(req)` | Per-arm provenance; filters are pushed down, never rewritten |
| Answer | `answer QUERY` | `engine.answer(req)` / `answer_stream` | Contract-checked; counts need exact aggregate evidence |
| Inspect | `status`, `doctor [--probe]` | `engine.status()` | Active generation, coverage gaps, backend probes |
| Evaluate | `evaluate FILE` | `su_multi_arm.evaluation.evaluate` | Recall per labelled query on your own labels |
| Config | `config upgrade / validate / schema` | `load_config(path)` | v1 files upgrade deterministically; all problems reported at once |

Next: write your own configuration ([reference](CONFIGURATION.md)), pick [backends](backends.md)
and [providers](providers.md), or [serve](serving.md) the engine over HTTP or MCP.
