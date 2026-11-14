"""CLI entry point.

    python -m tracker collect                 # one full pass (run hourly)
    python -m tracker collect --only players,prices
    python -m tracker annotate 730 "Operation launch" --at 2026-11-10T18:00
    python -m tracker report 730 --days 7     # event impact table in the terminal
    python -m tracker demo                    # synthetic data in data/demo.db
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from . import analysis, collect, config, db


def _cmd_collect(args) -> int:
    steps = tuple(s.strip() for s in args.only.split(",")) if args.only else collect.ALL_STEPS
    bad = set(steps) - set(collect.ALL_STEPS)
    if bad:
        print(f"Unknown step(s): {', '.join(sorted(bad))}. Valid: {', '.join(collect.ALL_STEPS)}", file=sys.stderr)
        return 2
    stats = collect.run(steps)
    total = stats.players + stats.prices + stats.news + stats.owners + stats.metadata
    # Fail the job only if nothing at all was collected (e.g. Steam is down / we're blocked);
    # partial failures are logged but shouldn't spam CI with red runs.
    return 1 if total == 0 and stats.failures else 0


def _cmd_annotate(args) -> int:
    engine = db.get_engine()
    ts = datetime.fromisoformat(args.at) if args.at else db.utcnow()
    db.insert_ignore(engine, db.events, [{
        "appid": args.appid, "ts": ts.replace(tzinfo=None), "kind": "manual", "title": args.title,
        "url": args.url, "detail": "{}", "source_id": f"manual:{ts.isoformat()}:{args.title}"[:128],
    }])
    print(f"Added manual event for {args.appid} at {ts.isoformat()} UTC")
    return 0


def _cmd_report(args) -> int:
    engine = db.get_engine()
    players = analysis.load_players(engine, [args.appid])
    events = analysis.load_events(engine, [args.appid])
    impact = analysis.event_impact(players, events, window_days=args.days)
    if impact.empty:
        print("No events with player data yet.")
        return 0
    view = impact[["ts", "kind", "title", "before", "after", "change_pct", "enough_data"]].copy()
    view["before"] = view["before"].round(0)
    view["after"] = view["after"].round(0)
    view["change_pct"] = view["change_pct"].round(1)
    import pandas as pd
    with pd.option_context("display.max_colwidth", 50, "display.width", 160):
        print(view.to_string(index=False))
    return 0


def _cmd_demo(args) -> int:
    path = config.ROOT / "data" / "demo.db"
    if path.exists() and not args.keep:
        path.unlink()
    engine = db.get_engine(f"sqlite:///{path.as_posix()}")
    analysis_days = args.days
    from . import demo
    demo.generate(engine, days=analysis_days)
    print(f"Wrote synthetic data to {path}")
    print("View it with:  streamlit run dashboard/app.py -- --demo")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tracker", description="Steam market tracker")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="run one collection pass")
    c.add_argument("--only", help=f"comma list of steps: {','.join(collect.ALL_STEPS)}")
    c.set_defaults(func=_cmd_collect)

    a = sub.add_parser("annotate", help="add a manual event (e.g. a marketing beat or off-Steam patch)")
    a.add_argument("appid", type=int)
    a.add_argument("title")
    a.add_argument("--at", help="ISO timestamp in UTC (default: now)")
    a.add_argument("--url")
    a.set_defaults(func=_cmd_annotate)

    r = sub.add_parser("report", help="print before/after player impact of each event")
    r.add_argument("appid", type=int)
    r.add_argument("--days", type=int, default=7, help="window on each side of the event")
    r.set_defaults(func=_cmd_report)

    d = sub.add_parser("demo", help="generate synthetic data in data/demo.db")
    d.add_argument("--days", type=int, default=90)
    d.add_argument("--keep", action="store_true", help="don't wipe an existing demo.db")
    d.set_defaults(func=_cmd_demo)

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
