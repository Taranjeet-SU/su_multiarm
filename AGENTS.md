# Working in su_multi_arm

This repository is intended for public distribution. Use synthetic data, examples and fixtures only.
Never copy customer corpora, production prompts with private examples, embeddings, model checkpoints,
private endpoints or credentials into it. Keep live deployment repositories and running services untouched
unless a user separately requests deployment work.

Read docs/ARCHITECTURE.md and docs/PLAN.md before changing contracts. Distinguish implemented features,
mocked contracts, real-service integration checks and measured production outcomes. Do not silently
ignore unavailable providers, truncation, extraction errors or incompatible index recipes.

Update the generated docs/config.schema.json when Config changes. Run meaningful affected tests,
ruff check src tests, and wheel/sdist build for release changes. Optional external tests must use isolated
resources and must not run destructive commands against an existing database. Update docs/VALIDATION.md
with actual results; never infer relevance from an ingestion count or a tiny synthetic fixture.

## Local test workspace (owner instruction, 2026-10-12)

Both Codex and Claude must run local tests and benchmark experiments from `~/su_mutliarm_tests`
(the spelling is intentional). Keep reusable test/harness source and synthetic fixtures in this
repository, but place test working directories, temporary files, caches, downloaded models,
benchmark corpora/qrels, indexes, logs and generated results in that external workspace.
Use separate agent/run subdirectories to avoid collisions. Direct pytest temporary/cache paths and
model/download caches there, and prevent test execution from writing Python bytecode into this repo.
Do not relocate or delete earlier artifacts as part of this instruction.

The owner permits evaluation on public/open-source datasets in that external workspace. Verify the
selected dataset's terms and provenance before downloading; never commit its corpus or qrels here.
Only synthetic fixtures belong in the repository; sanitized aggregate validation reports may be
recorded in docs with their actual scope and limitations. This permission does not authorize paid
provider use, changes to live services, publication, or private/customer-data evaluation.
CI should use its own isolated temporary workspace rather than depend on this machine's home path.
