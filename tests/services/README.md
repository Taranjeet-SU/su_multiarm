# Real-service test stack

Throwaway containers for the **real-service** test tier (`docs/plans/v2_implementation_plan.md`
§6.1, CI job `services` in §6.2): OpenSearch, Elasticsearch, PostgreSQL with pgvector, Neo4j and
MongoDB. The generic SQL backend (`sqlalchemy`) is tested on SQLite (no service) and on the PostgreSQL container.

**These are isolated, disposable instances.** Every port binds to `127.0.0.1` on a non-default port,
data lives on tmpfs or inside the container, and `down -v` deletes everything. The `SU_TEST_*`
variables below must only ever point at this stack (or an equally disposable instance you created for
the purpose). Never point them at an existing or shared database: the tests create, overwrite and
delete tables, indexes, labels and collections under their own prefixes, and `test_external.py`
deliberately refuses to run when the variables are unset for that reason.

## Start, wait, stop

```bash
# from the repository root
docker compose -p su-multi-arm-tests -f tests/services/docker-compose.yml up -d --wait
python tests/services/wait_ready.py            # protocol-level readiness table, exit 1 on timeout
docker compose -p su-multi-arm-tests -f tests/services/docker-compose.yml down -v
```

`--wait` blocks until every container's healthcheck passes (`--wait-timeout 420` is a sensible cap
on a cold pull). `wait_ready.py` is standard-library only and probes the published host ports the
way a client would (HTTP cluster health, PostgreSQL startup message, Bolt handshake, Mongo `ping`),
so it also works when the stack was started elsewhere. First start pulls roughly 5 GB of images.

## Host ports and connection variables

Host ports come from environment variables with non-default defaults so the stack never collides with
a service already listening on the standard port. Set them before `up` and before running tests.

| Service | Port variable | Default | Variable the tests read | Value for the defaults |
|---|---|---|---|---|
| PostgreSQL + pgvector | `SU_TEST_POSTGRES_PORT` | 15432 | `SU_TEST_POSTGRES_DSN` | `postgresql://su_test:su_test_password@127.0.0.1:15432/su_test` |
| Neo4j (Bolt) | `SU_TEST_NEO4J_BOLT_PORT` | 17687 | `SU_TEST_NEO4J_URI`, `SU_TEST_NEO4J_PASSWORD` | `bolt://127.0.0.1:17687`, `su_test_password` (user `neo4j`) |
| Neo4j (HTTP browser) | `SU_TEST_NEO4J_HTTP_PORT` | 17474 | — (debugging only) | `http://127.0.0.1:17474` |
| OpenSearch | `SU_TEST_OPENSEARCH_PORT` | 19200 | `SU_TEST_OPENSEARCH_URL` | `http://127.0.0.1:19200` (security plugin disabled) |
| OpenSearch 3.x (profile `opensearch3`) | `SU_TEST_OPENSEARCH3_PORT` | 19202 | `SU_TEST_OPENSEARCH_URL` (+ optional `SU_TEST_OPENSEARCH_MAJOR=3`) | `http://127.0.0.1:19202` (security plugin disabled) |
| Elasticsearch | `SU_TEST_ES_PORT` | 19201 | `SU_TEST_ELASTICSEARCH_URL` | `http://127.0.0.1:19201` (xpack security disabled) |
| MongoDB | `SU_TEST_MONGO_PORT` | 27018 | `SU_TEST_MONGO_URI` | `mongodb://su_test:su_test_password@127.0.0.1:27018/?authSource=admin` |

`SU_TEST_POSTGRES_DSN` is read by `tests/conformance/test_postgres_conformance.py`, the
`sql[postgres]` cases of `tests/conformance/test_sql_conformance.py` (same server through
`postgresql+psycopg://`) and the engine test in `tests/test_external.py`; the conformance suites fall
back to the stack default and skip, with the reason shown, when the variable is unset and the default
is unreachable (the engine test needs the variable set). Each test uses a fresh random table prefix
and drops only the schemas/tables that prefix created. `SU_TEST_MONGO_URI` is read by
`tests/conformance/test_mongodb_conformance.py`; `SU_TEST_NEO4J_URI` and `SU_TEST_NEO4J_PASSWORD` by
`tests/conformance/test_neo4j_conformance.py` and `tests/services/test_neo4j_engine.py` (each uses a
fresh `namespace` and drops only that namespace). For both services: when the URI variable is unset
the tests fall back to the stack default above and skip, with the reason shown, if that default is
unreachable; when it is set, an unreachable server is a failure. `SU_TEST_OPENSEARCH_URL` is read by the OpenSearch conformance and adapter
tests (`tests/conformance/` with `-m external`, `tests/services/test_opensearch_adapter.py`) and
defaults to `http://127.0.0.1:19200`; every test uses its own random `su_test_<hex>` index prefix and
removes it afterwards. `SU_TEST_ELASTICSEARCH_URL` is read the same way by the Elasticsearch conformance
tests (`tests/conformance/` with `-m external`, plus `tests/conformance/test_elasticsearch_conformance.py`)
and defaults to `http://127.0.0.1:19201`.

The credentials (`su_test` / `su_test_password`) are defined only in `docker-compose.yml` and are not
secrets. Change them there if you change them anywhere.

## OpenSearch 2.x and 3.x

The default `opensearch` service is 2.19.1. A second service, `opensearch3`
(`opensearchproject/opensearch:3.9.0`, port 19202), sits behind the compose profile `opensearch3`
so a plain `up` starts the same stack as before. The OpenSearch tests address one server at a time
through `SU_TEST_OPENSEARCH_URL`; `SU_TEST_OPENSEARCH_MAJOR` (optional, `2` or `3`) makes
`test_probe_reports_a_supported_major` assert which major answered, so a run cannot silently hit the
wrong container.

```bash
docker compose -p su-multi-arm-tests -f tests/services/docker-compose.yml --profile opensearch3 up -d --wait opensearch3
python tests/services/wait_ready.py --only opensearch3
SU_TEST_OPENSEARCH_URL=http://127.0.0.1:19202 SU_TEST_OPENSEARCH_MAJOR=3 \
  PYTHONDONTWRITEBYTECODE=1 pytest -q -m external -k opensearch -p no:cacheprovider \
  --basetemp="$HOME/su_mutliarm_tests/<agent>/pytest_tmp" \
  tests/services/test_opensearch_adapter.py tests/conformance
docker compose -p su-multi-arm-tests -f tests/services/docker-compose.yml --profile opensearch3 rm -sfv opensearch3
```

Both images run with `DISABLE_SECURITY_PLUGIN=true` and `DISABLE_INSTALL_DEMO_CONFIG=true`, so
neither needs `OPENSEARCH_INITIAL_ADMIN_PASSWORD` (required since 2.12 only when the demo security
configuration is installed). Both bundle their JDK (3.x needs JDK 21). `wait_ready.py` checks
`opensearch3` only when it is named with `--only`.

## Running the external tests

```bash
python -m pip install -e '.[dev,postgres,sql,neo4j,mongodb,opensearch,elasticsearch]'
export SU_TEST_POSTGRES_DSN='postgresql://su_test:su_test_password@127.0.0.1:15432/su_test'
export SU_TEST_NEO4J_URI='bolt://127.0.0.1:17687'
export SU_TEST_NEO4J_PASSWORD='su_test_password'
export SU_TEST_OPENSEARCH_URL='http://127.0.0.1:19200'
export SU_TEST_ELASTICSEARCH_URL='http://127.0.0.1:19201'
export SU_TEST_MONGO_URI='mongodb://su_test:su_test_password@127.0.0.1:27018/?authSource=admin'
PYTHONDONTWRITEBYTECODE=1 pytest -q -m external -p no:cacheprovider \
    --basetemp="$HOME/su_mutliarm_tests/<agent>/pytest_tmp"
```

Keep pytest temporary paths outside the repository, as `AGENTS.md` requires. In CI
(`.github/workflows/services.yml`) the same commands run with `$RUNNER_TEMP`.

## What the compose file does and does not do

- Pinned image tags: `opensearchproject/opensearch:2.19.1`, `opensearchproject/opensearch:3.9.0`
  (profile `opensearch3`; digest recorded in the compose file),
  `docker.elastic.co/elasticsearch/elasticsearch:8.17.4`, `pgvector/pgvector:0.8.0-pg17`,
  `neo4j:5.26.0-community`, `mongo:7.0.16`. Record the resolved digests in `docs/VALIDATION.md`
  when you report results (`docker compose ... images --format json`).
- Security disabled where the vendor dev image allows it: OpenSearch via `DISABLE_SECURITY_PLUGIN=true`,
  Elasticsearch via `xpack.security.enabled=false`. Both are plain HTTP without authentication and are
  dev-only switches. PostgreSQL, Neo4j and MongoDB have no such switch, so they use the throwaway
  user/password above.
- Single-node, memory-capped: both Lucene engines run with `discovery.type=single-node` (which skips
  the production bootstrap checks, so the host's `vm.max_map_count` is left untouched) and a 512 MiB
  JVM heap inside a 1.5 GiB container limit; Neo4j runs with a 512 MiB heap and 128 MiB page cache.
  Plan on roughly 5 GB of RAM for the whole stack.
- No persistent volumes: PostgreSQL, Neo4j and MongoDB data directories are tmpfs; OpenSearch and
  Elasticsearch keep data inside the container's writable layer. `down -v` removes anything the images
  declare as anonymous volumes. Restarting the stack starts empty.
- The compose project name is fixed to `su-multi-arm-tests` so `up`/`down` only ever touch containers
  this file created. Verify with `docker ps` before and after if other containers run on the host.
