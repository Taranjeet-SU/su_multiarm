"""Pattern library files, loader with decisions replay, ownership precedence, lint and identity
(addendum §3.3, §3.4.3; owner policy amendment 2026-11-04)."""

from __future__ import annotations

import copy

import pytest

from su_multi_arm.extraction.patterns.library import PatternLibraryError, load_library
from su_multi_arm.extraction.patterns.writer import append_rows
from tests.pattern_fixtures import CONNECT, FORMAT, engine_or_skip, write_library, write_system

AT = "2026-11-04T12:00:00Z"


@pytest.fixture(params=["re2", "regex"])
def engine(request):
    return engine_or_skip(request.param)


def message(pid: str, regex: str, **extra) -> dict:
    row = {
        "id": pid,
        "kind": "message",
        "status": extra.pop("status", "approved"),
        "regex": regex,
        "signature": {"id": pid, "category": "test"},
    }
    row.update(extra)
    return row


TIMEOUT = message("msg.timeout", r"^request (?P<rid>[a-z0-9]+) timed out$", examples={"positive": ["request r1 timed out"]})


def decision(op: str, pid: str, by: str = "user:alice", **extra) -> dict:
    return {"op": op, "id": pid, "by": by, "at": AT, **extra}


def issue_codes(library, severity=None) -> set[str]:
    return {i.code for i in library.issues if severity is None or i.severity == severity}


def test_example_library_loads_and_orders_by_priority(tmp_path, engine):
    library = load_library(write_library(tmp_path, [CONNECT, FORMAT, TIMEOUT]), engine)
    assert [e.entry.id for e in library.approved()] == ["fmt.kv_v1", "msg.dependency.connect_failed", "msg.timeout"]
    assert library.errors() == [] and "unanchored" in issue_codes(library, "warning")
    assert library.name == "synthetic-core" and len(library.identity()) == 64


def test_proposals_take_effect_only_through_decisions(tmp_path, engine):
    root = write_library(tmp_path, [FORMAT])
    proposal = message("msg.timeout", TIMEOUT["regex"], status="proposed", examples=TIMEOUT["examples"])
    write_system(root, "proposed.yaml", [proposal])
    assert "msg.timeout" not in {e.entry.id for e in load_library(root, engine).approved()}
    write_system(root, "decisions.yaml", [decision("approve", "msg.timeout", by="system:auto")])
    library = load_library(root, engine)
    assert library.entries["msg.timeout"].status == "approved" and len(library.applied_decisions) == 1
    write_system(root, "decisions.yaml", [decision("approve", "msg.timeout", by="system:auto"), decision("deprecate", "msg.timeout")])
    assert load_library(root, engine).entries["msg.timeout"].status == "deprecated"


def test_supersede_shadows_the_target_without_editing_it(tmp_path, engine):
    root = write_library(tmp_path, [FORMAT])
    old = message("msg.old", r"^disk (?P<d>[a-z]+) full$", examples={"positive": ["disk sda full"]})
    new = message("msg.new", r"^disk (?P<d>[a-z0-9]+) is full$", status="proposed", examples={"positive": ["disk sda1 is full"]})
    write_system(root, "learned.yaml", [old])
    write_system(root, "proposed.yaml", [new])
    before = (root / "learned.yaml").read_bytes()
    write_system(root, "decisions.yaml", [decision("supersede", "msg.new", by="system:auto", target="msg.old")])
    library = load_library(root, engine)
    assert library.entries["msg.old"].status == "superseded" and library.entries["msg.new"].status == "approved"
    assert (root / "learned.yaml").read_bytes() == before


def test_expire_drops_an_unpromoted_proposal_only(tmp_path, engine):
    root = write_library(tmp_path, [FORMAT, TIMEOUT])
    proposal = message("msg.p", r"^queue (?P<q>[a-z]+) stalled$", status="proposed", examples={"positive": ["queue q stalled"]})
    write_system(root, "proposed.yaml", [proposal])
    write_system(root, "decisions.yaml", [decision("expire", "msg.p", by="system:retention"), decision("expire", "msg.timeout")])
    library = load_library(root, engine)
    assert library.entries["msg.p"].status == "expired"
    assert library.entries["msg.timeout"].status == "approved" and "expire_not_proposal" in issue_codes(library, "warning")


def test_human_entry_wins_over_learned_and_proposed(tmp_path, engine):
    root = write_library(tmp_path, [FORMAT, TIMEOUT])
    learned = message("msg.timeout", r"^req (?P<rid>[a-z0-9]+) timed out$", examples={"positive": ["req r1 timed out"]})
    write_system(root, "learned.yaml", [learned])
    library = load_library(root, engine)
    assert library.entries["msg.timeout"].owner == "user" and library.entries["msg.timeout"].regex == TIMEOUT["regex"]
    assert "shadowed_by_owner" in issue_codes(library, "info")


def test_human_disable_wins_over_everything(tmp_path, engine):
    root = write_library(tmp_path, [FORMAT])
    write_system(root, "learned.yaml", [TIMEOUT])
    write_system(root, "decisions.yaml", [decision("disable", "msg.timeout"), decision("approve", "msg.timeout", by="system:auto"), decision("approve", "msg.timeout")])
    library = load_library(root, engine)
    assert library.entries["msg.timeout"].status == "disabled" and "disabled_final" in issue_codes(library, "info")


def test_system_cannot_disable_or_change_human_entries(tmp_path, engine):
    root = write_library(tmp_path, [FORMAT, TIMEOUT])
    write_system(root, "learned.yaml", [message("msg.l", r"^cpu (?P<c>[0-9]+) hot$", examples={"positive": ["cpu 3 hot"]})])
    write_system(root, "decisions.yaml", [decision("deprecate", "msg.timeout", by="system:auto"), decision("disable", "msg.l", by="system:auto")])
    library = load_library(root, engine)
    assert library.entries["msg.timeout"].status == "approved" and library.entries["msg.l"].status == "approved"
    assert {"system_decision_on_user_entry", "system_disable"} <= issue_codes(library, "warning")
    assert library.applied_decisions == []


def test_unknown_decision_target_is_an_error(tmp_path, engine):
    root = write_library(tmp_path, [FORMAT])
    write_system(root, "decisions.yaml", [decision("approve", "msg.nowhere")])
    with pytest.raises(PatternLibraryError, match="unknown_decision_target"):
        load_library(root, engine)


def lint_error(tmp_path, engine, patterns, code, **header):
    with pytest.raises(PatternLibraryError) as info:
        load_library(write_library(tmp_path, patterns, **header), engine)
    assert code in {i.code for i in info.value.issues if i.severity == "error"}, info.value


def test_lint_attribute_endpoint_with_trigger(tmp_path, engine):
    bad = copy.deepcopy(CONNECT)
    bad["relations"].append({"subject": "$service", "predicate": "emits", "object": "dep", "trigger_group": "trigger"})
    lint_error(tmp_path, engine, [FORMAT, bad], "attribute_trigger")


def test_lint_attribute_endpoint_in_relations(tmp_path, engine):
    bad = copy.deepcopy(CONNECT)
    bad["relations"] = [{"subject": "$service", "predicate": "emits", "object": "dep"}]
    lint_error(tmp_path, engine, [FORMAT, bad], "attribute_relation")


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda e: e["entities"].append({"group": "nope", "type": "x"}), "undeclared_group"),
        (lambda e: e["relations"].append({"subject": "client", "predicate": "p", "object": "ms"}), "relation_endpoint"),
        (lambda e: e["attribute_links"].append({"subject": "$host", "predicate": "p", "object": "$signature"}), "attribute_endpoint"),
        (lambda e: e["examples"]["positive"].append("db-client connected to redis"), "example_fails"),
        (lambda e: e["examples"]["negative"].append("db-client refused by kafka"), "example_fails"),
        (lambda e: e["examples"]["positive"].append("mail ops@example.com: db-client refused by kafka"), "example_pii"),
        (lambda e: e.update(regex="(?P<client>[a-z]+-client) to {{vocab:nothing}}"), "unresolved_template"),
        (lambda e: e.update(regex=r"(?P<client>(?:[a-z]+)+-client) refused by (?P<dep>kafka)"), "star_height"),
        (lambda e: e.pop("signature"), "message_signature"),
        (lambda e: e.update(kind="annotate", signature=None), "relations_kind"),
        (lambda e: e.update(then={"field_from_group": "dep"}), "then_not_format"),
        (lambda e: e.update(kind="sideways"), "schema"),
        (lambda e: e.update(status="maybe"), "schema"),
        (lambda e: e["fields"].update(ms={"type": "float"}), "schema"),
    ],
)
def test_lint_errors(tmp_path, engine, mutate, code):
    bad = copy.deepcopy(CONNECT)
    mutate(bad)
    lint_error(tmp_path, engine, [FORMAT, bad], code)


def test_lint_duplicate_id_within_an_owner(tmp_path, engine):
    lint_error(tmp_path, engine, [FORMAT, TIMEOUT, TIMEOUT], "duplicate_id")


def test_lint_ambiguity(tmp_path, engine):
    a = message("msg.a", r"^job (?P<j>[a-z]+) failed$", examples={"positive": ["job x failed"]})
    b = message("msg.b", r"^job (?P<j>[a-z0-9]+) failed$", examples={"positive": ["job y failed"]})
    lint_error(tmp_path, engine, [a, b], "ambiguous")
    b["priority"] = 1
    assert "ambiguous" not in issue_codes(load_library(write_library(tmp_path, [a, b]), engine))
    b["priority"], b["applies_to"], a["applies_to"] = 0, {"service": ["scheduler"]}, {"service": ["doc-fetcher"]}
    assert "ambiguous" not in issue_codes(load_library(write_library(tmp_path, [a, b]), engine))


def test_vocabulary_and_mask_templates_expand(tmp_path, engine):
    entry = message("msg.svc", r"^service (?P<svc>{{vocab:service}}) took (?P<ms>{{mask:num}}) ms$", examples={"positive": ["service doc-fetcher took 12.5 ms"]})
    library = load_library(write_library(tmp_path, [entry]), engine)
    regex = library.entries["msg.svc"].regex
    assert "(?:ingest-worker|query-gateway|doc-fetcher|scheduler)" in regex  # longest first, then by name
    assert "(?:-?[0-9]+(?:\\.[0-9]+)?)" in regex and "{{" not in regex


def test_broken_proposals_stay_inert_but_approving_them_fails(tmp_path, engine):
    root = write_library(tmp_path, [FORMAT])
    write_system(root, "proposed.yaml", [message("msg.bad", r"^x (?P<a>\S+)\S+$", status="proposed")])
    library = load_library(root, engine)
    assert "inactive_unbounded_follow" in issue_codes(library, "warning") and library.errors() == []
    write_system(root, "decisions.yaml", [decision("approve", "msg.bad")])
    with pytest.raises(PatternLibraryError, match="unbounded_follow"):
        load_library(root, engine)


def test_hand_edit_of_a_system_file_is_reported(tmp_path, engine):
    root = write_library(tmp_path, [FORMAT])
    append_rows(root, "proposed.yaml", [message("msg.p", r"^queue (?P<q>[a-z]+) stalled$", status="proposed")])
    assert "system_file_edited" not in issue_codes(load_library(root, engine))
    with open(root / "proposed.yaml", "a") as handle:
        handle.write("# hand note\n")
    assert "system_file_edited" in issue_codes(load_library(root, engine), "warning")


def test_identity_tracks_effective_entries_only(tmp_path, engine):
    root = write_library(tmp_path, [FORMAT, TIMEOUT])
    first = load_library(root, engine).identity()
    commented = dict(TIMEOUT, examples={"positive": ["request r1 timed out", "request r2 timed out"]}, provenance={"note": "x"})
    assert load_library(write_library(tmp_path, [FORMAT, commented]), engine).identity() == first
    write_system(root, "proposed.yaml", [message("msg.p", r"^queue (?P<q>[a-z]+) stalled$", status="proposed")])
    assert load_library(root, engine).identity() == first
    write_system(root, "decisions.yaml", [decision("approve", "msg.p")])
    assert load_library(root, engine).identity() != first


def test_missing_files_are_typed_errors(tmp_path, engine):
    with pytest.raises(PatternLibraryError, match="does not exist"):
        load_library(tmp_path / "absent", engine)
    tmp_path.joinpath("lib").mkdir()
    with pytest.raises(PatternLibraryError, match="library.yaml"):
        load_library(tmp_path / "lib", engine)
    root = write_library(tmp_path / "lib2", [FORMAT])
    (root / "decisions.yaml").write_text("op: approve\n")
    with pytest.raises(PatternLibraryError, match="sequence"):
        load_library(root, engine)
