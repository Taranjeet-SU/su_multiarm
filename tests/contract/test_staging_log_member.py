"""Log intents over the partitioned log member equal the episode-archive deployment (staging fix B1).

The same synthetic episode summaries (``tests/two_halves_support.py``: failure and success-heartbeat
families, closed days 2026-11-04..08, the provisional 2026-11-09 up to 10:30 UTC) are served two
ways:

* today's single-member log deployment: the episode archive file (closed days) plus the provisional
  store (``two_halves_support.build``), and
* the unified collection: the adopted support store plus the live log member whose closed days are
  archive day members and whose today is a live slice (``staging_support.heartbeat_collection``).

``issue_history``, ``log_liveness`` and ``incident_episodes`` return the same rows, tiers and
coverage on both. Synthetic only; no provider call, no service.
"""

from __future__ import annotations

import pytest

import tests.live_log_support as L
import tests.staging_support as S
from su_multi_arm.engine import Engine
from tests import two_halves_support as th


@pytest.fixture(scope="module")
def clock():
    mp = pytest.MonkeyPatch()
    mp.setattr(Engine, "_clock", staticmethod(lambda zone: th.NOW.astimezone(zone)))
    yield
    mp.undo()


@pytest.fixture(scope="module")
def archive_engine(tmp_path_factory, clock):
    tmp = tmp_path_factory.mktemp("archive_side")
    return th.build(tmp, th.unified_config(tmp))


@pytest.fixture(scope="module")
def member_engine(tmp_path_factory, clock):
    return S.heartbeat_collection(tmp_path_factory.mktemp("member_side"), L.fixture())


def out(engine: Engine, name: str, args: dict) -> dict:
    return engine.call_tool(name, args).result.output.model_dump(mode="json")


HISTORY_ROW = ("signature_id", "tenant", "service", "category", "occurrences", "episodes", "first_failure", "last_failure", "active_days", "tiers", "provisional", "partial")


@pytest.mark.parametrize("args", [
    {"window": "last 3 days"},
    {"window": "last 7 days", "group_by": ["tenant", "signature_id"]},
    {"window": "last 7 days", "group_by": ["service"], "list_episodes": 10},
    {"window": "last 3 days", "tenant": "brightmoor", "list_episodes": 5},
    {"window": "last 3 days", "category": ["success"]},
])
def test_issue_history_equals_the_episode_archive(archive_engine, member_engine, args):
    a, m = out(archive_engine, "issue_history", args), out(member_engine, "issue_history", args)
    assert a["status"] == m["status"] and a["window"] == m["window"]
    assert [{k: r.get(k) for k in HISTORY_ROW} for r in m["rows"]] == [{k: r.get(k) for k in HISTORY_ROW} for r in a["rows"]]
    ca, cm = a["history"]["coverage"], m["history"]["coverage"]
    assert cm["tiers"] == ca["tiers"] and cm["archive_days"] == ca["archive_days"] and sorted(cm["provisional_days"]) == sorted(ca["provisional_days"])
    assert cm["missing_days"] == ca["missing_days"] and cm["boundary_unknown_pieces"] == ca["boundary_unknown_pieces"]
    piece = ("doc_id", "tenant", "service", "signature_id", "first_failure", "last_failure", "occurrences", "is_continuation", "tier", "provisional")
    assert [{k: p.get(k) for k in piece} for p in m["history"]["episodes"]] == [{k: p.get(k) for k in piece} for p in a["history"]["episodes"]]


@pytest.mark.parametrize("args", [{"service": ["crawler"]}, {"service": ["report-worker"], "recovery": True}, {"tenant": "brightmoor"}])
def test_log_liveness_equals_the_episode_archive(archive_engine, member_engine, args):
    a, m = out(archive_engine, "log_liveness", args), out(member_engine, "log_liveness", args)
    assert m["status"] == a["status"] and m["rendering"] == a["rendering"]
    assert m["rows"] == a["rows"]
    assert m["history"]["coverage"]["heartbeat_days"] == a["history"]["coverage"]["heartbeat_days"]


def test_liveness_reads_halted_and_running_crawls_from_the_member(member_engine):
    m = out(member_engine, "log_liveness", {"service": ["crawler"]})
    status = {r["tenant"]: r["status"] for r in m["rows"]}
    assert status == {"alderbank-stg": "halted", "brightmoor": "running"}
    assert all(r["provisional"] for r in m["rows"])  # today is a live slice


def test_incident_episodes_equal_the_episode_archive_on_closed_days(archive_engine, member_engine):
    args = {"window": "last 3 days", "as_of": "2026-11-08"}
    a, m = out(archive_engine, "incident_episodes", args), out(member_engine, "incident_episodes", args)
    keys = ("tenant", "service", "signature_id", "first_failure", "last_failure", "occurrences", "evidence_id")
    assert m["rows"] and [{k: r.get(k) for k in keys} for r in m["rows"]] == [{k: r.get(k) for k in keys} for r in a["rows"]]
    assert m["source_unavailable"] == a["source_unavailable"]


def test_the_member_serves_today_s_episodes_through_its_live_slice(member_engine):
    """The episode archive deployment serves today only through issue_history/liveness (the
    provisional store); the member's live slice is records like any other day."""
    m = out(member_engine, "incident_episodes", {"window": "today"})
    assert {r["signature_id"] for r in m["rows"]} >= {"crawler.fetch_timeout", "report.queue_stalled", "auth.unauthorized"}


def test_log_top_equals_the_episode_archive_on_closed_days(archive_engine, member_engine):
    args = {"by": "service", "window": "last 3 days", "as_of": "2026-11-08"}
    a, m = out(archive_engine, "log_top", args), out(member_engine, "log_top", args)
    assert m["rows"] and m["rows"] == a["rows"] and m["status"] == a["status"]


def test_the_two_halves_planner_gets_an_operations_half_over_the_member(tmp_path, clock):
    from su_multi_arm import SearchRequest

    engine = S.heartbeat_collection(tmp_path, L.fixture(), planner=True)
    result = engine.search(SearchRequest(query="Has the crawl stopped? It is supposed to run every 30 minutes.", mode="rules"))
    coverage = result.trace["source_coverage"]
    assert coverage["plan"]["halves"] == ["knowledge", "operations"]
    intents = {a["intent"]: a for a in result.aggregates if a.get("intent")}
    assert "log_liveness" in intents and intents["log_liveness"]["status"] != "refused"
    status = {r["tenant"]: r["status"] for r in intents["log_liveness"]["rows"]}
    assert status == {"alderbank-stg": "halted", "brightmoor": "running"}
