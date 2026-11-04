"""QA findings for the released 2.0.0a1 (docs/plans/reviews/qa_2.0.0a1.md).

Every test here reproduces one confirmed finding and is marked ``xfail(strict=True)`` with the
finding id, so the default suite stays green while the bug exists and turns red (XPASS) once it is
fixed. Model replies are replayed offline through an ``httpx.MockTransport`` patched into the
provider's HTTP call (no network, no key); the single paid test (QA-08) is opt-in: it needs
``OPENAI_API_KEY`` and ``SU_QA_PAID=1`` and is skipped otherwise. Synthetic data only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import yaml

from su_multi_arm import Document, Engine, SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import ConfigError

pytestmark = pytest.mark.qa

ROOT = Path(__file__).resolve().parents[2]
PRODUCTS = ["CloudTax", "SearchDesk", "LedgerOne"]
STATUSES = ["open", "resolved", "pending"]


# --- helpers ------------------------------------------------------------------------------------


def corpus(n: int = 36) -> list[Document]:
    """Deterministic synthetic tickets; every (product, status) pair has n/9 records."""
    docs = []
    for i in range(n):
        product, status = PRODUCTS[i % 3], STATUSES[(i // 3) % 3]
        docs.append(
            Document.model_validate(
                {
                    "id": f"case:{i:04d}",
                    "dataset": "qa",
                    "source": "case",
                    "title": f"{product} rounding ticket {i}",
                    "fields": {"description": f"Customer reports a rounding problem in {product}. Status {status}."},
                    "metadata": {"product": product, "status": status, "priority": 1 + i % 3, "created_at": f"2025-01-{1 + i % 28:02d}T00:00:00Z"},
                }
            )
        )
    return docs


def config_dict(state: Path, **extra) -> dict:
    data = {
        "schema_version": 2,
        "dataset": "qa",
        "backends": {"local": {"kind": "sqlite", "path": str(state)}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": {
            "fields": [
                {"name": "product", "type": "string", "filterable": True, "listable": True, "groupable": True},
                {"name": "status", "type": "string", "filterable": True, "listable": True, "groupable": True},
                {"name": "priority", "type": "integer", "filterable": True, "groupable": True},
                {"name": "created_at", "type": "datetime", "filterable": True, "sortable": True, "time_basis": "created"},
            ]
        },
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}}},
        "pipeline": {"decision": {"kind": "rules"}, "arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local"}, "structured": {"backend": "local"}}},
    }
    for key, value in extra.items():
        data[key] = value
    return data


def openai_role(max_tokens: int = 500, structured_output: str = "native") -> dict:
    return {"provider": "openai", "model": "gpt-4o-mini", "api_key_env": "OPENAI_API_KEY", "max_tokens": max_tokens, "capabilities": {"structured_output": structured_output}}


def chat_reply(content: dict, prompt_tokens: int = 100, completion_tokens: int = 20) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "gpt-4o-mini-2024-07-18",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(content)}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    )


@pytest.fixture
def fake_openai(monkeypatch):
    """Route every provider HTTP call to ``handler(schema_name, payload) -> dict`` (offline)."""
    from su_multi_arm.providers import base

    monkeypatch.setenv("OPENAI_API_KEY", "sk-qa-offline-not-a-key")
    state: dict = {"handler": None, "delay": 0.0, "requests": 0}

    def transport_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        name = ((body.get("response_format") or {}).get("json_schema") or {}).get("name", "")
        payload = json.loads(body["messages"][-1]["content"])
        state["requests"] += 1
        if state["delay"]:
            time.sleep(state["delay"])
        return chat_reply(state["handler"](name, payload))

    original = base.HttpCall.__init__

    def patched(self, transport=None):
        original(self, httpx.MockTransport(transport_handler))

    monkeypatch.setattr(base.HttpCall, "__init__", patched)
    return state


def grounded_answer(text: str, kind: str, payload: dict, cite_aggregate: bool) -> dict:
    ids = [e["id"] for e in payload.get("evidence", [])]
    cite = [i for i in ids if i.startswith("aggregate:")] if cite_aggregate else [i for i in ids if not i.startswith("aggregate:")][:3]
    return {"answer": text, "claims": [{"text": text, "kind": kind, "evidence_ids": cite}], "citations": cite, "insufficient_evidence": False}


# --- QA-01 -------------------------------------------------------------------------------------


def test_qa01_llm_count_with_unbound_qualifier_is_not_passed(tmp_path, fake_openai):
    # Replays the observed gpt-4o-mini behaviour: for "How many CloudTax tickets have status open?"
    # the planner proposed a structured count on status=open only (the run counted 111 = every
    # open record; the CloudTax-open truth was 38) and the contract passed the count claim.
    roles = {"embedder": {"provider": "hash", "dimensions": 64}, "planner": openai_role(structured_output="json_schema"), "generator": openai_role()}
    cfg = validate_config(config_dict(tmp_path / "s", models={"roles": roles}, pipeline={"decision": {"kind": "llm"}, "arms": {"lexical": {"backend": "local"}, "structured": {"backend": "local"}}}))
    engine = Engine(cfg)
    engine.ingest(corpus())

    def handler(name, payload):
        if name == "LlmPlan":
            return {"steps": [{"arm": "structured", "scope": {"predicates": [{"field": "status", "op": "eq", "value": "open"}], "measure": {"kind": "count"}}}]}
        return grounded_answer("There are 12 open CloudTax tickets.", "count", payload, cite_aggregate=True)

    fake_openai["handler"] = handler
    result = engine.answer(SearchRequest(query="How many CloudTax tickets have status open?"))
    counted = result.retrieval.aggregates[0]["value"]
    truth = sum(1 for d in corpus() if d.metadata["product"] == "CloudTax" and d.metadata["status"] == "open")
    assert counted != truth  # the replayed plan counts the wider population (precondition)
    assert result.contract == "abstained" or any("CloudTax" in u.constraint_text for u in result.unsupported_constraints)


def test_qa09_planner_aggregate_exposes_applied_predicates(tmp_path, fake_openai):
    roles = {"embedder": {"provider": "hash", "dimensions": 64}, "planner": openai_role(structured_output="json_schema")}
    cfg = validate_config(config_dict(tmp_path / "s", models={"roles": roles}, pipeline={"decision": {"kind": "llm"}, "arms": {"lexical": {"backend": "local"}, "structured": {"backend": "local"}}}))
    engine = Engine(cfg)
    engine.ingest(corpus())
    fake_openai["handler"] = lambda name, payload: {
        "steps": [{"arm": "structured", "scope": {"predicates": [{"field": "status", "op": "eq", "value": "open"}], "measure": {"kind": "count"}}}]
    }
    aggregate = engine.search(SearchRequest(query="How many tickets have status open?")).aggregates[0]
    assert aggregate["value"] == 12  # precondition: the scope was applied
    assert "status" in json.dumps({k: v for k, v in aggregate.items() if k != "document_ids"})


# --- QA-02 -------------------------------------------------------------------------------------


def test_qa02_type_mismatched_request_filter_is_flagged(tmp_path):
    engine = Engine(validate_config(config_dict(tmp_path / "s")))
    engine.ingest(corpus())
    as_int = engine.search(SearchRequest(query="x", filters={"priority": 3}, aggregate={"operation": "count"})).aggregates[0]
    assert as_int["value"] == 12  # precondition: the integer filter counts correctly
    result = engine.search(SearchRequest(query="x", filters={"priority": "3"}, aggregate={"operation": "count"}))
    aggregate = result.aggregates[0]
    flagged = bool(result.unsupported_constraints) or bool(aggregate["unsupported"]) or aggregate["exactness"] != "exact"
    assert flagged or aggregate["value"] == 12


def test_qa02_undeclared_filter_field_is_rejected_or_flagged(tmp_path):
    engine = Engine(validate_config(config_dict(tmp_path / "s")))
    engine.ingest(corpus())
    try:
        result = engine.search(SearchRequest(query="rounding", filters={"prodcut": "CloudTax"}))
    except (ValueError, ConfigError):
        return
    assert result.hits or result.unsupported_constraints


def test_qa02_false_absence_passes_contract(tmp_path, fake_openai):
    roles = {"embedder": {"provider": "hash", "dimensions": 64}, "generator": openai_role()}
    engine = Engine(validate_config(config_dict(tmp_path / "s", models={"roles": roles})))
    engine.ingest(corpus())
    fake_openai["handler"] = lambda name, payload: grounded_answer("There are no priority 3 tickets.", "count", payload, cite_aggregate=True)
    result = engine.answer(SearchRequest(query="Are there any priority 3 tickets?", filters={"priority": "3"}, aggregate={"operation": "count"}))
    assert result.contract == "abstained" or result.unsupported_constraints


# --- QA-03 -------------------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["the latest release notes", "every time the service restarts", "What are all the steps to add an exemption certificate?"])
def test_qa03_documented_ordinary_questions_are_retrieval(tmp_path, query):
    data = yaml.safe_load((ROOT / "examples/v2/local.yaml").read_text())
    data["backends"]["local"]["path"] = str(tmp_path / "state")
    engine = Engine(validate_config(data))
    records = [json.loads(line) for line in (ROOT / "examples/documents.jsonl").read_text().splitlines() if line.strip()]
    engine.ingest([Document.model_validate(r) for r in records])
    result = engine.search(SearchRequest(query=query))
    assert result.trace["decision"]["outcome"] == "decided", result.trace["decision"]


# --- QA-04 -------------------------------------------------------------------------------------


def test_qa04_count_in_answer_text_without_exact_scope(tmp_path, fake_openai):
    roles = {"embedder": {"provider": "hash", "dimensions": 64}, "planner": openai_role(structured_output="json_schema"), "generator": openai_role()}
    cfg = validate_config(config_dict(tmp_path / "s", models={"roles": roles}, pipeline={"decision": {"kind": "llm"}, "arms": {"lexical": {"backend": "local"}, "structured": {"backend": "local"}}}))
    engine = Engine(cfg)
    engine.ingest(corpus())

    def handler(name, payload):
        if name == "LlmPlan":
            return {"steps": [{"arm": "lexical", "query": "pending LedgerOne tickets"}]}
        return {
            "answer": "There are exactly 2 pending LedgerOne tickets: case:0005 and case:0014. That is the complete list.",
            "claims": [{"text": "Pending LedgerOne tickets were found.", "kind": "narrative", "evidence_ids": [payload["evidence"][0]["id"]]}],
            "citations": [payload["evidence"][0]["id"]],
            "insufficient_evidence": False,
        }

    fake_openai["handler"] = handler
    # 2.0.0a2: with QA-03 fixed the list grammar needs a structured target ("record" is a generic
    # unit; "ticket" is not declared in this schema), so the question names the unit.
    result = engine.answer(SearchRequest(query="List every pending LedgerOne record."))
    assert result.unsupported_constraints  # precondition: no exact scope was executed for the list intent
    assert result.contract == "abstained"


# --- QA-05 -------------------------------------------------------------------------------------


def test_qa05_concurrent_session_turns_are_all_recorded(tmp_path):
    engine = Engine(validate_config(config_dict(tmp_path / "s")))
    engine.ingest(corpus())
    sid = engine.create_session()
    threads = [threading.Thread(target=engine.search, args=(SearchRequest(query=f"rounding {i}"),), kwargs={"session_id": sid}) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(engine.session(sid).record.turns) == 20


def test_qa05_session_call_budget_holds_under_concurrency(tmp_path, fake_openai):
    roles = {"embedder": {"provider": "hash", "dimensions": 64}, "generator": openai_role()}
    budgets = {"max_llm_calls": 5, "session": {"max_llm_calls": 2}}
    engine = Engine(validate_config(config_dict(tmp_path / "s", models={"roles": roles}, budgets=budgets)))
    engine.ingest(corpus())
    fake_openai["handler"] = lambda name, payload: grounded_answer("Rounding is configured per line.", "narrative", payload, cite_aggregate=False)
    fake_openai["delay"] = 0.3
    sid = engine.create_session()
    errors: list[BaseException] = []

    def turn(i):
        try:
            engine.answer(SearchRequest(query=f"How is rounding fixed {i}?"), session_id=sid)
        except BaseException as exc:  # BudgetExhausted is the expected refusal
            errors.append(exc)

    threads = [threading.Thread(target=turn, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fake_openai["requests"] <= 2, f"{fake_openai['requests']} model calls under a session limit of 2"


# --- QA-06 -------------------------------------------------------------------------------------


def test_qa06_llm_extraction_is_not_capped_by_per_request_budget(tmp_path, fake_openai):
    roles = {"embedder": {"provider": "hash", "dimensions": 64}, "extractor": openai_role()}
    cfg = validate_config(
        config_dict(tmp_path / "s", models={"roles": roles}, extraction={"extractors": ["llm"], "labels": ["product"]})
    )
    assert cfg.budgets.max_llm_calls == 12  # documented as "per request"
    fake_openai["handler"] = lambda name, payload: {"entities": [], "relations": []}
    manifest = Engine(cfg).ingest(corpus(20))
    assert manifest["status"] == "complete"


# --- QA-07 -------------------------------------------------------------------------------------


@pytest.mark.parametrize("word", ["Straße", "ﬁle"])
def test_qa07_lexical_matches_its_own_indexed_word(tmp_path, word):
    data = config_dict(tmp_path / "s")
    data["pipeline"]["arms"] = {"lexical": {"backend": "local"}}
    engine = Engine(validate_config(data))
    engine.ingest([Document.model_validate({"id": "d1", "dataset": "qa", "title": "t", "fields": {"body": f"Die {word} ist gesperrt."}})])
    assert [h.document_id for h in engine.search(SearchRequest(query=word)).hits] == ["d1"]


# --- QA-08 (paid, opt-in) ------------------------------------------------------------------------


@pytest.mark.external
@pytest.mark.skipif(not (os.environ.get("OPENAI_API_KEY") and os.environ.get("SU_QA_PAID") == "1"), reason="paid: needs OPENAI_API_KEY and SU_QA_PAID=1")
def test_qa08_narrative_questions_mostly_answer(tmp_path):
    roles = {"embedder": {"provider": "hash", "dimensions": 64}, "generator": {**openai_role(max_tokens=600), "max_tokens": 600}}
    engine = Engine(validate_config(config_dict(tmp_path / "s", models={"roles": roles}, budgets={"max_llm_calls": 2})))
    engine.ingest(corpus())
    questions = [f"How do I fix a {p} rounding problem?" for p in PRODUCTS] + ["What causes rounding problems?", "How was the rounding issue resolved?"]
    abstained = sum(engine.answer(SearchRequest(query=q)).contract == "abstained" for q in questions)
    assert abstained <= 2


# --- QA-10 / QA-11 / QA-12 / QA-13 / QA-14 / QA-15 ---------------------------------------------------


def run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    code = "import sys; from su_multi_arm.cli import main; sys.argv = ['su-multi-arm'] + sys.argv[1:]; main()"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-c", code, *args], cwd=cwd, capture_output=True, text=True, env=env, timeout=120)


def test_qa10_upgraded_config_opens_the_v1_index(tmp_path):
    shutil.copytree(ROOT / "examples", tmp_path / "examples", ignore=shutil.ignore_patterns("state", "*state*"))
    assert run_cli("--config", "examples/local.yaml", "ingest", "examples/documents.jsonl", cwd=tmp_path).returncode == 0
    assert run_cli("--config", "examples/local.yaml", "config", "upgrade", "--output", "local_v2.yaml", cwd=tmp_path).returncode == 0
    search = run_cli("--config", "local_v2.yaml", "search", "exemption certificate", cwd=tmp_path)
    assert search.returncode == 0, search.stderr[-300:]


def test_qa11_config_validate_reports_all_errors_without_traceback(tmp_path):
    (tmp_path / "bad.yaml").write_text("schema_version: 2\ndataset: x\nbogus_one: 1\nbogus_two: 2\n")
    proc = run_cli("--config", "bad.yaml", "config", "validate", cwd=tmp_path)
    assert proc.returncode != 0
    output = proc.stdout + proc.stderr
    assert "Traceback" not in output
    assert "bogus_one" in output and "bogus_two" in output


def test_qa12_bearer_scheme_is_case_insensitive(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from su_multi_arm.api import create_app

    monkeypatch.setenv("QA_TOKEN", "qa-token-value")
    engine = Engine(validate_config(config_dict(tmp_path / "s")))
    engine.ingest(corpus(9))
    client = TestClient(create_app(engine=engine, token_env="QA_TOKEN"))
    assert client.post("/v2/search", json={"query": "rounding"}, headers={"Authorization": "Bearer qa-token-value"}).status_code == 200
    assert client.post("/v2/search", json={"query": "rounding"}, headers={"Authorization": "bearer qa-token-value"}).status_code == 200


def test_qa13_openai_native_planner_is_valid_and_sends_a_strict_schema(tmp_path, monkeypatch, fake_openai):
    # 2.0.0a2 fixed QA-13 by making LlmPlan strict-expressible (the preferred fix), so a native OpenAI
    # planner is now *valid* and its request carries strict: true. Validation still rejects a native
    # role whose schema is not strict-expressible (tests/unit/test_provider_capabilities.py).
    roles = {"embedder": {"provider": "hash", "dimensions": 64}, "planner": openai_role(structured_output="native")}
    engine = Engine(validate_config(config_dict(tmp_path / "s", models={"roles": roles}, pipeline={"decision": {"kind": "llm"}, "arms": {"lexical": {"backend": "local"}}})))
    engine.ingest(corpus(9))
    sent: list[dict] = []
    fake_openai["handler"] = lambda name, payload: sent.append(name) or {"steps": [{"arm": "lexical", "query": "rounding", "filters": [], "scope": None}]}
    result = engine.search(SearchRequest(query="How is rounding fixed?"))
    assert sent == ["LlmPlan"] and result.trace["decision"]["outcome"] == "decided" and result.hits


def test_qa14_offline_ingest_without_query_time_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    roles = {"embedder": {"provider": "hash", "dimensions": 64}, "planner": openai_role(structured_output="json_schema")}
    cfg = validate_config(config_dict(tmp_path / "s", models={"roles": roles}, pipelines={"planned": {"decision": {"kind": "llm"}}}, request={"pipelines": ["planned"]}))
    assert Engine(cfg).ingest(corpus(9))["status"] == "complete"


def test_qa15_strict_schema_refuses_keywords_openai_rejects():
    from typing import Annotated, Literal, Union

    from pydantic import BaseModel, Field

    from su_multi_arm.providers.base import StrictSchemaError, strict_schema

    class A(BaseModel):
        kind: Literal["a"]
        x: int

    class B(BaseModel):
        kind: Literal["b"]
        y: str

    class Wrapper(BaseModel):
        item: Annotated[Union[A, B], Field(discriminator="kind")]

    # Keywords the OpenAI strict endpoint answered 400 for (2026-11-03): oneOf, allOf, discriminator,
    # uniqueItems, const without type. 2.0.0a2: refused with a typed error listing each of them.
    with pytest.raises(StrictSchemaError) as error:
        strict_schema(Wrapper.model_json_schema(), "Wrapper")
    assert "'oneOf'" in str(error.value) and "'discriminator'" in str(error.value)
