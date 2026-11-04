"""The UI's "copy CLI command" text (``ui/src/lib/cli.ts``, addendum A2 N13) round-trips: every shared
case's expected text is POSIX ``shlex.quote`` of its argv, ``shlex.split`` restores the argv, and the
real CLI parser reads the same op, id, author and reason (hostile ids and reasons included). The
vitest suite asserts the TypeScript builder produces exactly the same text."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from su_multi_arm.cli import build_parser

CASES = json.loads((Path(__file__).resolve().parents[2] / "ui" / "src" / "lib" / "cli_cases.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=[c["input"]["id"][:20] for c in CASES])
def test_copy_command_round_trips_through_shlex_and_the_cli_parser(case):
    given = case["input"]
    argv = shlex.split(case["expected"])
    assert case["expected"] == " ".join(shlex.quote(token) for token in argv)
    assert argv[0] == "su-multi-arm"
    args = build_parser().parse_args(argv[1:])
    assert (args.command, args.patterns_command) == ("patterns", given["op"])
    assert (str(args.config), args.id, args.by, args.reason) == (given["config"], given["id"], given["by"], given["reason"])


def test_cases_cover_shell_metacharacters_and_option_lookalikes():
    texts = " ".join(c["input"]["reason"] + c["input"]["id"] for c in CASES)
    for needle in ("'", "$(", "`", "&&", "|", ";", "\n", "*"):
        assert needle in texts
    assert any(c["input"]["id"].startswith("-") for c in CASES) and any(c["input"]["reason"].startswith("-") for c in CASES)
