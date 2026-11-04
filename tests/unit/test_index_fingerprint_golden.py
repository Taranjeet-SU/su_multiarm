"""Index fingerprints of the shipped examples are pinned to their 2.0.0a1 values (a2 review B2).

An index built by 2.0.0a1 must open under a later release when the effective settings did not
change. The values below were computed with the published 2.0.0a1 tree (``public-main``) by the
same procedure (backend ``path`` values fixed to ``/golden/state`` so the hash does not depend on
the checkout location). A release that changes them on purpose must bump ``INDEX_FORMAT``, give
``IndexNotReady`` a specific message and say so in the changelog; it updates this table then.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from su_multi_arm.core.config import load_config
from su_multi_arm.extraction.base import BuiltinExtractor
from su_multi_arm.ingestion.write import INDEX_FORMAT, WritePath

ROOT = Path(__file__).resolve().parents[2]

A1_FINGERPRINTS = {
    "examples/anthropic.yaml": "986c5e0ea71939b5b9a9845a27ebd770ad5a71cbfd52a32783c2fb8e9e39649c",
    "examples/local.yaml": "ff976a6a623771ab4e22072ed31081cc61b3d5d8cc9b3356f0efcf08a86a0f8c",
    "examples/mapping.yaml": "fc1cbfb089d20750095d53bf2e4b4c066b0b500bf09e328e08d266555277366a",
    "examples/postgres_neo4j.yaml": "e5b1e0566fad36e283a59f72730fa6a4fac6dd5025f13d3b2a0a6b40b872d102",
    "examples/semantic.yaml": "fb1591f611fe98c9d855f491bcf23bb004f61d0703c94d726e2f823328ba2e06",
    "examples/v2/hosted_models.yaml": "f01cd798f2df1bfc434c20c811af7ea71bfff3a470bae1bdf1ca09e8b740eb79",
    "examples/v2/local.yaml": "3eb1abab8e99e774eeac334b89491898f7ae9face8fab45036bdd8c0362b56e9",
    "examples/v2/mixed_backends.yaml": "94f647bd919c087759d800ec9000683e4396891cc5750117a4c688447409680c",
}


def example_fingerprint(path: Path) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        config = load_config(path, check_plugins=False, warn=False)
    for name, backend in list(config.backends.items()):
        if hasattr(backend, "path"):
            config.backends[name] = backend.model_copy(update={"path": Path("/golden/state")})
    role = getattr(config.models.roles, "extractor", None)
    extractor = BuiltinExtractor(config.extraction, SimpleNamespace(providers={"extractor": SimpleNamespace(config=role)} if role else {}))
    storage, arms = config.storage, config.pipeline.arms
    slots = {"catalog": storage.catalog, "records": storage.records, "ledger": storage.ledger, "communities": storage.communities}
    for slot, arm in (("lexical", arms.lexical), ("vector", arms.dense), ("graph", arms.graph)):
        if arm:
            slots[slot] = arm.backend
    return WritePath.index_fingerprint(SimpleNamespace(cfg=config, extractor=extractor, slots=slots))


def test_every_shipped_example_is_pinned():
    shipped = {str(p.relative_to(ROOT)) for p in [*ROOT.glob("examples/*.yaml"), *ROOT.glob("examples/v2/*.yaml")]}
    assert shipped == set(A1_FINGERPRINTS), "a new example needs its golden fingerprint recorded here"


@pytest.mark.parametrize("example", sorted(A1_FINGERPRINTS))
def test_index_fingerprint_equals_the_2_0_0a1_value(example):
    assert INDEX_FORMAT == 3, "INDEX_FORMAT changed: update the golden table and the changelog deliberately"
    assert example_fingerprint(ROOT / example) == A1_FINGERPRINTS[example]
