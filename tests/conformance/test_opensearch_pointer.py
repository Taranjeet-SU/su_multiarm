"""OpenSearch dataset aliases as a secondary generation pointer: compare-and-set activation
(risk R1). ``external``: reads ``SU_TEST_OPENSEARCH_URL`` (default the throwaway stack); unset and
unreachable skips, set and unreachable fails. Uses its own index prefix and deletes only it."""

from __future__ import annotations

import os
import secrets

import pytest

from su_multi_arm.core.errors import BackendError, WriteConflict
from su_multi_arm.testing import backend_conformance as kit

from .conftest import OPENSEARCH_URL, opensearch_backend

pytestmark = pytest.mark.external
if not os.environ.get("SU_TEST_OPENSEARCH_URL"):
    pytest.importorskip("opensearchpy")  # without the extra and without a configured server: skip; set: fail


@pytest.fixture
def backend():
    probe = opensearch_backend("su_probe")
    try:
        probe.probe()
    except BackendError as exc:
        if os.environ.get("SU_TEST_OPENSEARCH_URL"):
            raise
        pytest.skip(f"no OpenSearch at {OPENSEARCH_URL} ({exc})")
    finally:
        probe.close()
    made = opensearch_backend(f"su_test_{secrets.token_hex(4)}")
    yield made
    rows = made.transport.request("GET", f"/_cat/indices/{made.prefix}_*", params={"format": "json", "h": "index"})
    names = [row["index"] for row in rows or []]
    if names:
        made.transport.request("DELETE", "/" + ",".join(names))
    made.close()


def test_alias_pointer_moves_only_from_the_expected_generation(backend):
    gen3 = kit.GEN.model_copy(update={"generation": "c" * 32})
    for gen in (kit.GEN, kit.GEN2, gen3):
        kit.seed(backend, gen)
    with pytest.raises(WriteConflict) as info:
        backend.activate_generation(kit.GEN, expected=kit.GEN2.generation)
    assert info.value.reason == "stale_base" and backend.active_generation("conformance") is None
    backend.activate_generation(kit.GEN, expected=None)
    with pytest.raises(WriteConflict):
        backend.activate_generation(kit.GEN2, expected=None)
    backend.activate_generation(kit.GEN2, expected=kit.GEN.generation)  # remove ... must_exist accepted
    with pytest.raises(WriteConflict):
        backend.activate_generation(gen3, expected=kit.GEN.generation)
    assert backend.active_generation("conformance") == kit.GEN2.generation


def test_a_concurrent_alias_switch_fails_the_whole_request(backend, monkeypatch):
    """The read-and-compare passes, but the alias moved before the ``_aliases`` request: the
    ``must_exist`` remove of the expected generation's index fails the request atomically."""
    gen3 = kit.GEN.model_copy(update={"generation": "c" * 32})
    for gen in (kit.GEN, kit.GEN2, gen3):
        kit.seed(backend, gen)
    backend.activate_generation(kit.GEN2)
    monkeypatch.setattr(backend, "active_generation", lambda dataset: kit.GEN.generation)  # stale read
    with pytest.raises(WriteConflict):
        backend.activate_generation(gen3, expected=kit.GEN.generation)
    monkeypatch.undo()
    assert backend.active_generation("conformance") == kit.GEN2.generation
