"""Serve the UI against an engine built on the synthetic logs fixtures (smoke / e2e harness).

    PYTHONPATH=src:tests:. python ui/e2e/serve_synthetic.py --workdir <dir> --port 18099 [--fixture small|gold] [--theme theme.json]

``small`` (default): two synthetic days through ``LogsPipeline.run_daily`` (``tests/logs_support.py``).
``gold``: the 10-day synthetic gold set under the ``logs_example`` profile, with the log tools,
co-firing incidents and the history the overview dashboard reads (``tests/logs_gold_support.py``).
Both use the loopback ``FakeChatServer`` as the streaming generator (contract-mocked model, no paid
calls), two synthetic noise proposals for the review view, and bearer auth (token from
``SU_UI_SMOKE_TOKEN``, default ``smoke-token``). Synthetic data only; everything under ``--workdir``.
"""

from __future__ import annotations

import argparse
import os
import shutil
from datetime import date
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "python" / "su_multi_arm_ui" / "static"


def add_proposals(library: Path) -> None:
    from su_multi_arm.extraction.patterns.library import PatternEntry
    from su_multi_arm.extraction.patterns.writer import append_proposal

    gate = {"passed": True, "engine": "regex", "matched": 17, "checks": {"support": {"passed": True, "detail": "17 lines"}, "out_of_cluster": {"passed": True, "detail": "0 lines"}}}
    for ident, regex, example in (("noise.metrics_flush", r"^metrics flushed in [0-9]{1,4}ms$", "metrics flushed in 12ms"), ("noise.cache_warm", r"^cache warm-up done for <ipv4>$", "cache warm-up done for <ipv4>")):
        append_proposal(library, PatternEntry(id=ident, kind="noise", status="proposed", regex=regex, examples={"positive": [example]}, evidence={"gate": gate}, provenance={"author": "system:synthetic"}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--fixture", choices=["small", "gold"], default="small")
    parser.add_argument("--theme", type=Path, help="a UI theme JSON file (serving.ui.theme)")
    args = parser.parse_args()

    import uvicorn

    from su_multi_arm.core.config import validate_config
    from su_multi_arm.engine import Engine
    from su_multi_arm.serving.http import create_app
    from su_multi_arm.testing.provider_fakes import FakeChatServer

    work = args.workdir.expanduser().resolve()
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    os.environ.setdefault("SU_UI_SMOKE_TOKEN", "smoke-token")
    fake = FakeChatServer(delay=0.02).__enter__()
    serving = {"http": {"bind": f"127.0.0.1:{args.port}", "auth": "bearer_env", "token_env": "SU_UI_SMOKE_TOKEN"}, "ui": {"enabled": True, **({"theme": str(args.theme.resolve())} if args.theme else {})}}
    if args.fixture == "gold":
        from tests.logs_gold_support import build_gold, gold_config

        data = gold_config(work)
        data["models"]["roles"]["generator"] = fake.role(streaming=True)
        data["serving"] = serving
        engine, _pipe, _report = build_gold(work, data=data)
        add_proposals(work / "patterns")
    else:
        from logs_support import Clock, cluster_for, config_data, days, pipeline  # tests/logs_support.py

        window = days(date(2026, 1, 18), args.days)
        cluster = cluster_for(window)
        clock = Clock(window[0])
        for day in window:
            clock.set_day(day)
            pipe, _cfg, _backend = pipeline(work, cluster, now=clock)
            pipe.run_daily(day)
        add_proposals(work / "patterns")
        data = config_data(work)
        data["models"]["roles"]["generator"] = fake.role(streaming=True)
        data["serving"] = serving
        engine = Engine(validate_config(data))
    app = create_app(engine=engine, ui_static_dir=STATIC)
    print(f"UI: http://127.0.0.1:{args.port}/ui/  token: ${{SU_UI_SMOKE_TOKEN}}", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        fake.__exit__(None, None, None)


if __name__ == "__main__":
    main()
