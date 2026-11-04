"""Serve the UI against an engine built on the synthetic logs fixtures (smoke / e2e harness).

    PYTHONPATH=src:tests python ui/e2e/serve_synthetic.py --workdir ~/su_mutliarm_tests/<agent>/ui-smoke --port 8099

Builds two synthetic days with ``LogsPipeline.run_daily`` (fake log cluster, no network), starts the
loopback ``FakeChatServer`` as the streaming generator (contract-mocked model, no paid calls), and
serves ``create_app`` with the UI bundle under bearer auth (token from ``SU_UI_SMOKE_TOKEN``, default
``smoke-token``). Synthetic data only; everything is written under ``--workdir``.
"""

from __future__ import annotations

import argparse
import os
import shutil
from datetime import date
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--days", type=int, default=2)
    args = parser.parse_args()

    import uvicorn

    from logs_support import Clock, cluster_for, config_data, days, pipeline  # tests/logs_support.py
    from su_multi_arm.core.config import validate_config
    from su_multi_arm.engine import Engine
    from su_multi_arm.serving.http import create_app
    from su_multi_arm.testing.provider_fakes import FakeChatServer

    work = args.workdir.expanduser().resolve()
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    window = days(date(2026, 1, 18), args.days)
    cluster = cluster_for(window)
    clock = Clock(window[0])
    for day in window:
        clock.set_day(day)
        pipe, _cfg, _backend = pipeline(work, cluster, now=clock)
        pipe.run_daily(day)
    # Two synthetic noise proposals waiting for review (the pattern review view).
    from su_multi_arm.extraction.patterns.library import PatternEntry
    from su_multi_arm.extraction.patterns.writer import append_proposal

    gate = {"passed": True, "engine": "regex", "matched": 17, "checks": {"support": {"passed": True, "detail": "17 lines"}, "out_of_cluster": {"passed": True, "detail": "0 lines"}}}
    for ident, regex, example in (("noise.metrics_flush", r"^metrics flushed in [0-9]{1,4}ms$", "metrics flushed in 12ms"), ("noise.cache_warm", r"^cache warm-up done for <ipv4>$", "cache warm-up done for <ipv4>")):
        append_proposal(work / "patterns", PatternEntry(id=ident, kind="noise", status="proposed", regex=regex, examples={"positive": [example]}, evidence={"gate": gate}, provenance={"author": "system:synthetic"}))
    os.environ.setdefault("SU_UI_SMOKE_TOKEN", "smoke-token")
    fake = FakeChatServer(delay=0.02).__enter__()
    data = config_data(work)
    data["models"]["roles"]["generator"] = fake.role(streaming=True)
    data["serving"] = {"http": {"bind": f"127.0.0.1:{args.port}", "auth": "bearer_env", "token_env": "SU_UI_SMOKE_TOKEN"}, "ui": {"enabled": True}}
    engine = Engine(validate_config(data))
    static = Path(__file__).resolve().parents[1] / "python" / "su_multi_arm_ui" / "static"
    app = create_app(engine=engine, ui_static_dir=static)
    print(f"UI: http://127.0.0.1:{args.port}/ui/  token: ${{SU_UI_SMOKE_TOKEN}}", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        fake.__exit__(None, None, None)


if __name__ == "__main__":
    main()
