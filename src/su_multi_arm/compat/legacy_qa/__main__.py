"""``python -m su_multi_arm.compat.legacy_qa --profile PROFILE.yaml [--bind 127.0.0.1:8110]``."""

from __future__ import annotations

import argparse

LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m su_multi_arm.compat.legacy_qa", description="Serve a legacy QA contract on package engines.")
    parser.add_argument("--profile", required=True, help="legacy-QA compatibility profile (YAML/JSON)")
    parser.add_argument("--bind", default="127.0.0.1:8110", help="host:port (loopback by default; the adapter has no inbound auth)")
    parser.add_argument("--allow-non-loopback", action="store_true", help="bind outside loopback (put an authenticating proxy in front)")
    args = parser.parse_args(argv)
    host, _, port = args.bind.rpartition(":")
    if host not in LOOPBACK and not args.allow_non_loopback:
        parser.error("the legacy adapter has no inbound authentication; bind to loopback or pass --allow-non-loopback")
    import uvicorn

    from . import create_legacy_app, load_profile

    app = create_legacy_app(load_profile(args.profile))
    # A bounded graceful stop (keep it below the service manager's stop timeout): in-flight answers
    # get to finish or are cancelled, so their spend reservations are committed or released.
    uvicorn.run(app, host=host.strip("[]"), port=int(port), log_level="info", timeout_graceful_shutdown=20)


if __name__ == "__main__":
    main()
