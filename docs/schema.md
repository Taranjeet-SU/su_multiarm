# Configuration JSON schema

The configuration v2 model (`su_multi_arm.core.config.Config`) is published as a JSON schema,
generated from code with `su_multi_arm.core.schema.write_all`:

- [config.schema.json](config.schema.json) — `schema_version: 2` (current)
- [config_v1.schema.json](config_v1.schema.json) — the legacy v1 model, kept while v1 files load
  through the upgrade path

Point your editor's YAML language server at the schema for completion and validation, or print it
from the CLI:

```bash
su-multi-arm --config examples/v2/local.yaml config schema
```

Schema validation is static. The engine adds checks a schema cannot express (capability of the named
backend, configured model roles, budgets); run `config validate` for those.
