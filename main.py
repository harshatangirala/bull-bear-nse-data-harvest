#!/usr/bin/env python3
"""Bull Bear NSE Data Harvest -- command line entry point.

    python main.py status                 what the DB knows right now
    python main.py load-estimate          NSE request-load budget
    python main.py verify-endpoints       re-probe every endpoint in the registry
    python main.py universe               show the alert universe
    python main.py once gainers week52_high
    python main.py once --all --dry-run   one full cycle, nothing dispatched
    python main.py report daily --date 2026-08-28
    python main.py run                    long-running scheduler
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

from bbnse.core.config import load_config
from bbnse.core.logging_setup import get_logger, setup_logging

log = get_logger("bbnse.cli")


def _parse_date(text: str | None) -> date | None:
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise SystemExit(f"Could not parse date '{text}'. Use YYYY-MM-DD.")


# --------------------------------------------------------------------------
def cmd_status(args, cfg) -> int:
    from bbnse.reports.base import to_local
    from bbnse.storage.dao import Dao

    dao = Dao(cfg.db_url)
    dao.create_all()
    print(f"\nDatabase: {cfg.db_url}\n")
    print("Table counts")
    for table, count in dao.stats().items():
        print(f"  {table:<20s} {count:>10,}")

    health = dao.all_health()
    print("\nFetcher health")
    if not health:
        print("  (nothing has run yet)")
    for h in sorted(health, key=lambda x: x.category):
        last = (to_local(h.last_success_at).strftime("%d-%b %H:%M")
                if h.last_success_at else "never")
        flag = "  OK " if h.consecutive_failures == 0 else "FAIL"
        print(f"  [{flag}] {h.category:<16s} last ok {last:>13s}  "
              f"rows {h.last_row_count:>5}  fails {h.consecutive_failures}")
        if h.last_error:
            print(f"          last error: {h.last_error[:90]}")
    print()
    return 0


def cmd_verify(args, cfg) -> int:
    """Re-probe every resolved endpoint. Catches NSE migrations early."""
    from bbnse.core.registry import count_rows, load_registry
    from bbnse.core.session import get_session

    registry = load_registry()
    session = get_session(cfg)
    session.bootstrap()

    ok = failed = unresolved = 0
    print(f"\nVerifying {len(registry)} endpoints against NSE...\n")
    for ep in sorted(registry, key=lambda e: e.name):
        if not ep.resolved:
            print(f"  [SKIP] {ep.name:<28s} UNRESOLVED (page: {ep.page})")
            unresolved += 1
            continue
        try:
            if ep.generation == "archive":
                res = session.get_file(ep.full_url, referer="/")
                rows = len(res.text.splitlines()) if res.ok else 0
            else:
                session.warm(ep.referer)
                res = session.get_json(ep.full_url, referer=ep.referer,
                                       params=ep.params or None)
                rows = count_rows(res.json, ep)
            if res.ok:
                print(f"  [ OK ] {ep.name:<28s} {res.status}  "
                      f"{len(res.content):>9,}B  rows={rows}")
                ok += 1
            else:
                print(f"  [FAIL] {ep.name:<28s} {res.status}  <-- check registry")
                failed += 1
        except Exception as exc:
            print(f"  [FAIL] {ep.name:<28s} {type(exc).__name__}: "
                  f"{str(exc)[:70]}")
            failed += 1

    print(f"\n  {ok} ok · {failed} failed · {unresolved} unresolved\n")
    if failed:
        print("  A failure usually means NSE migrated the page to its newer\n"
              "  /api/NextApi/ gateway. Re-discover with the recipe in\n"
              "  docs/endpoints.md, then update endpoints.yaml.\n")
    return 1 if failed else 0


def cmd_load_estimate(args, cfg) -> int:
    """Aggregate request load against NSE, computed from config.

    Reported two ways: what is actually running today, and what the full
    configured set will do once every fetcher is built. Payload sizes come
    from the `verified` bytes recorded in endpoints.yaml.
    """
    from bbnse.core.registry import load_registry
    from bbnse.pipeline import known_categories

    registry = load_registry()
    implemented = set(known_categories())
    tiers = cfg.section("schedule.tiers")
    assigned = cfg.section("schedule.categories")
    market = cfg.section("schedule.market")

    def _mins(a: str, b: str) -> float:
        (h1, m1), (h2, m2) = (map(int, a.split(":")), map(int, b.split(":")))
        return (h2 * 60 + m2) - (h1 * 60 + m1)

    market_min = _mins(market.get("open", "09:15"),
                       market.get("close", "15:30"))
    preopen_min = _mins(market.get("pre_open_start", "09:00"),
                        market.get("open", "09:15"))
    session_min = market_min + preopen_min

    def endpoint_bytes(cat: str) -> int:
        try:
            return int(registry.get(cat).verified.get("bytes", 0) or 0)
        except KeyError:
            return 0

    def report(scope: str, cats: set[str]) -> tuple[float, int, int]:
        rows, total_req, total_bytes = [], 0, 0
        for tier, spec in tiers.items():
            members = sorted(c for c, t in assigned.items()
                             if t == tier and c in cats)
            if not members:
                continue
            if "interval_sec" in spec:
                window = (preopen_min if tier == "pre_open" else market_min)
                cycles = (window * 60) / float(spec["interval_sec"])
                label = f"every {spec['interval_sec']}s"
            else:
                cycles, window = 1, 0
                label = f"once at {spec.get('at', '?')}"
            req = int(cycles * len(members))
            byts = int(cycles * sum(endpoint_bytes(c) for c in members))
            total_req += req
            total_bytes += byts
            rows.append((tier, label, len(members), int(cycles), req, byts))

        # Session overhead: one bootstrap per max-age, plus one warm per
        # distinct landing page each time the session is rebuilt.
        max_age = float(cfg.get("http.session_max_age_sec", 1800))
        bootstraps = max(1, int((session_min * 60) / max_age))
        referers = {registry.get(c).referer for c in cats
                    if c in registry and registry.get(c).resolved}
        overhead = bootstraps * (1 + len(referers))
        total_req += overhead

        print(f"\n  {scope}")
        print(f"  {'tier':<15}{'cadence':<18}{'cats':>5}{'cycles':>8}"
              f"{'requests':>10}{'MB':>9}")
        print("  " + "-" * 65)
        for tier, label, n, cycles, req, byts in rows:
            print(f"  {tier:<15}{label:<18}{n:>5}{cycles:>8}{req:>10}"
                  f"{byts / 1e6:>9.1f}")
        print(f"  {'session':<15}{'bootstrap+warm':<18}"
              f"{len(referers):>5}{bootstraps:>8}{overhead:>10}{'-':>9}")
        print("  " + "-" * 65)
        rate = total_req / session_min
        print(f"  {'TOTAL':<15}{'per trading day':<18}{len(cats):>5}"
              f"{'':>8}{total_req:>10}{total_bytes / 1e6:>9.1f}")
        print(f"  sustained rate: {rate:.2f} req/min "
              f"({rate / 60:.3f} req/sec) across the {session_min:.0f}-minute "
              f"session")
        return rate, total_req, total_bytes

    print("\nNSE request-load budget")
    print("=" * 69)
    now_rate, now_req, now_b = report(
        f"TODAY -- {len(implemented & set(assigned))} fetchers implemented",
        implemented & set(assigned))
    all_cats = set(assigned)
    tgt_rate, tgt_req, tgt_b = report(
        f"TARGET -- all {len(all_cats)} configured fetchers", all_cats)

    print("\n" + "=" * 69)
    print(f"  change: {now_rate:.2f} -> {tgt_rate:.2f} req/min "
          f"({tgt_rate / now_rate:.1f}x), "
          f"{now_req:,} -> {tgt_req:,} requests/day, "
          f"{now_b / 1e6:.0f} -> {tgt_b / 1e6:.0f} MB/day")

    # NSE publishes no official limit. The practical trigger for 429/403 is
    # sustained multi-request-per-second traffic, so the useful check is
    # whether we are anywhere near a per-second cadence.
    per_sec = tgt_rate / 60
    if per_sec >= 1.0:
        verdict = "RISK -- at or above 1 req/sec sustained. Raise intervals."
    elif per_sec >= 0.5:
        verdict = "CAUTION -- above 0.5 req/sec. Consider raising intervals."
    else:
        verdict = (f"OK -- {per_sec:.3f} req/sec is far below any plausible "
                   f"limit; a browser tab on the site does more.")
    print(f"  verdict: {verdict}\n")
    return 0


def cmd_universe(args, cfg) -> int:
    from bbnse.core.session import get_session
    from bbnse.core.universe import Universe

    uni = Universe(cfg, get_session(cfg))
    symbols = uni.build(force=args.refresh)
    print(f"\nUniverse mode : {uni.mode}")
    print(f"Symbols       : {len(symbols):,}")
    preview = sorted(symbols)[:24]
    print("Sample        : " + ", ".join(preview))
    if len(symbols) > 24:
        print(f"                ... and {len(symbols) - 24:,} more")
    print()
    return 0


def cmd_once(args, cfg) -> int:
    from bbnse.pipeline import Pipeline, known_categories

    categories = known_categories() if args.all else args.categories
    if not categories:
        print("Nothing to do. Pass category names or --all.")
        print(f"Known categories: {', '.join(known_categories())}")
        return 2

    unknown = [c for c in categories if c not in known_categories()]
    if unknown:
        print(f"Unknown categories: {', '.join(unknown)}")
        print(f"Known: {', '.join(known_categories())}")
        return 2

    pipe = Pipeline(cfg, dry_run=args.dry_run)
    if args.dry_run:
        print("\n*** DRY RUN -- alerts are computed but not dispatched ***")
    print(f"\nAlert universe: {len(pipe.universe):,} symbols "
          f"(mode: {pipe.universe.mode})")
    print(f"Market window : {pipe.calendar.window()}\n")

    results = pipe.run_categories(categories, force=args.force)

    print("\n" + "=" * 74)
    print(f"{'category':<16}{'rows':>7}{'signals':>9}{'merged':>8}"
          f"{'alerts':>8}   note")
    print("-" * 74)
    for r in results:
        note = r.error[:26] if r.error else (r.skipped[:26] if r.skipped else "")
        print(f"{r.category:<16}{r.rows:>7}{r.signals:>9}{r.merged:>8}"
              f"{r.alerts:>8}   {note}")
    print("=" * 74)
    total = sum(r.alerts for r in results)
    print(f"{total} alert(s) dispatched, "
          f"{sum(r.signals for r in results)} signal(s) raised, "
          f"{sum(r.merged for r in results)} merged as cross-feed duplicates.\n")
    return 0


def cmd_report(args, cfg) -> int:
    from bbnse.reports.daily import DailyReport
    from bbnse.reports.monthly import MonthlyReport
    from bbnse.reports.weekly import WeeklyReport
    from bbnse.storage.dao import Dao

    dao = Dao(cfg.db_url)
    dao.create_all()
    anchor = _parse_date(args.date) or date.today()

    builder = {"daily": DailyReport, "weekly": WeeklyReport,
               "monthly": MonthlyReport}[args.kind]
    report = builder(cfg, dao).generate(anchor)

    if args.print:
        print("\n" + report.markdown + "\n")
    else:
        print("\n" + report.summary + "\n")
    if report.md_path:
        print(f"  markdown : {report.md_path}")
        print(f"  html     : {report.html_path}\n")

    if args.send:
        from bbnse.notifiers.dispatch import Dispatcher
        Dispatcher(cfg, dao).send_report(report.title, report.summary,
                                         report.html_path)
        print("  sent to configured notifiers\n")
    return 0


def cmd_test_alert(args, cfg) -> int:
    """Send one synthetic alert through the real dispatch path."""
    from bbnse.notifiers.dispatch import Dispatcher
    from bbnse.processors.base import Signal
    from bbnse.processors.state import AlertDecision, event_key
    from bbnse.storage.dao import Dao

    dao = Dao(cfg.db_url)
    dao.create_all()
    sig = Signal(
        category="week52_high", rule_id="fresh_extreme", entity="TESTSTOCK",
        state_bucket="week52_high", severity="critical",
        title="▲ 52W HIGH TESTSTOCK @ 1,463.00",
        body="new high 1,463.00 | prev 1,441.50 (10-Jun-2026) | "
             "clears by +1.49% | day +6.65%",
        value=1463.0,
        payload={"symbol": "TESTSTOCK", "synthetic": True},
    )
    decision = AlertDecision(
        sig, "new",
        event_key(sig.category, sig.rule_id, sig.entity, sig.state_bucket),
    )
    disp = Dispatcher(cfg, dao)
    if not disp.channels:
        print("No notifier channels are enabled in config.yaml.")
        return 1
    print(f"\nDispatching one test alert to: "
          f"{', '.join(c.name for c in disp.channels)}\n")
    disp.dispatch([decision], trade_date=date.today())
    print("\nDone. If Telegram is enabled and nothing arrived, check that "
          ".env has TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.\n")
    return 0


def cmd_run(args, cfg) -> int:
    from bbnse.pipeline import Pipeline
    from bbnse.scheduler.runner import Runner

    runner = Runner(Pipeline(cfg, dry_run=args.dry_run))
    print("\nStarting Bull Bear NSE Data Harvest scheduler. Ctrl-C to stop.\n")
    runner.start(block=True)
    return 0


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="Bull Bear NSE Data Harvest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", help="path to config.yaml")
    p.add_argument("--log-level", default=None,
                   help="override app.log_level (DEBUG/INFO/WARNING)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show DB counts and fetcher health")
    sub.add_parser("verify-endpoints", help="re-probe every registry endpoint")
    sub.add_parser("test-alert", help="send one synthetic alert")
    sub.add_parser("load-estimate",
                   help="aggregate NSE request load, now vs full build-out")

    u = sub.add_parser("universe", help="show the alert universe")
    u.add_argument("--refresh", action="store_true",
                   help="re-download constituent lists")

    o = sub.add_parser("once", help="run one fetch/detect/notify cycle")
    o.add_argument("categories", nargs="*", help="category names")
    o.add_argument("--all", action="store_true", help="every known category")
    o.add_argument("--dry-run", action="store_true",
                   help="detect but do not dispatch")
    o.add_argument("--force", action="store_true",
                   help="process even if the payload is unchanged")

    r = sub.add_parser("report", help="generate a report")
    r.add_argument("kind", choices=["daily", "weekly", "monthly"])
    r.add_argument("--date", help="anchor date (YYYY-MM-DD), default today")
    r.add_argument("--print", action="store_true",
                   help="print the full markdown")
    r.add_argument("--send", action="store_true",
                   help="deliver via configured notifiers")

    run = sub.add_parser("run", help="start the long-running scheduler")
    run.add_argument("--dry-run", action="store_true",
                     help="detect but do not dispatch")
    return p


HANDLERS = {
    "status": cmd_status,
    "verify-endpoints": cmd_verify,
    "universe": cmd_universe,
    "once": cmd_once,
    "report": cmd_report,
    "test-alert": cmd_test_alert,
    "load-estimate": cmd_load_estimate,
    "run": cmd_run,
}


def _force_utf8_stdio() -> None:
    """Windows consoles default to cp1252, which cannot encode the arrows and
    severity emoji in alert text. Reconfigure rather than degrade output."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass   # not a real console (piped/redirected); fallback handles it


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(
        level=args.log_level or cfg.get("app.log_level", "INFO"),
        log_dir=cfg.abs_path(cfg.get("app.log_dir", "logs")),
    )
    try:
        return HANDLERS[args.command](args, cfg)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
