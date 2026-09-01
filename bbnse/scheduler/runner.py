"""Scheduling.

Two deliberate choices:

1. Cadence is per-tier, not one global timer. Live categories poll every
   couple of minutes but only inside market hours; daily categories fire once
   after close. Every job re-checks the live NSE trading calendar before doing
   work, so holidays and weekends cost nothing.

2. Catch-up. On an intermittent laptop the machine is often off at 18:30, so
   on startup we look back over recent trading days and run any daily job
   whose result was never recorded. Intraday gaps are logged but not
   backfilled -- NSE does not serve historical intraday snapshots.
"""
from __future__ import annotations

import signal
import threading
from datetime import date, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..core.logging_setup import get_logger
from ..pipeline import Pipeline, known_categories
from ..reports.daily import DailyReport
from ..reports.monthly import MonthlyReport
from ..reports.weekly import WeeklyReport

log = get_logger(__name__)

_DAY_ABBR = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5,
             "sun": 6}


def _hhmm(text: str, default: str) -> tuple[int, int]:
    parts = str(text or default).split(":")
    return int(parts[0]), int(parts[1])


class Runner:
    def __init__(self, pipeline: Pipeline | None = None):
        self.pipeline = pipeline or Pipeline()
        self.cfg = self.pipeline.cfg
        self.cal = self.pipeline.calendar
        self.sched = BackgroundScheduler(timezone=str(self.cal.tz))
        self._stop = threading.Event()
        self._last_session_date: date | None = None

    # -- category grouping ---------------------------------------------------
    def _tier_map(self) -> dict[str, list[str]]:
        """Config-declared categories, grouped by tier.

        Categories named in config but not yet implemented are skipped rather
        than raising: Phase 2 lands one fetcher at a time, and the schedule
        declares the full target set up front so the load budget is honest.
        """
        known = set(known_categories())
        tiers: dict[str, list[str]] = {}
        pending: list[str] = []
        for category, tier in (self.cfg.section("schedule.categories")
                               or {}).items():
            if category not in known:
                pending.append(category)
                continue
            tiers.setdefault(tier, []).append(category)
        if pending:
            log.info("categories scheduled but not yet implemented",
                     extra={"pending": sorted(pending)})
        return tiers

    # -- jobs ----------------------------------------------------------------
    def _run_tier(self, tier: str, categories: list[str],
                  required_window: str | None) -> None:
        window = self.cal.window()
        if required_window and window != required_window:
            log.debug("tier skipped, wrong window",
                      extra={"tier": tier, "window": window,
                             "want": required_window})
            return

        # First cycle of a new session clears yesterday's open states.
        today = self.cal.now().date()
        if required_window == "market_hours" and self._last_session_date != today:
            self.pipeline.on_session_open(today)
            self._last_session_date = today

        results = self.pipeline.run_categories(categories)
        for r in results:
            if r.error:
                log.error("cycle error", extra={"category": r.category,
                                                "err": r.error})
        total_alerts = sum(r.alerts for r in results)
        if total_alerts:
            log.info("tier produced alerts",
                     extra={"tier": tier, "alerts": total_alerts})

    def _run_daily_categories(self, tier: str, categories: list[str],
                              run_date: date | None = None) -> None:
        run_date = run_date or self.cal.now().date()
        job_id = f"tier:{tier}"
        if not self.cal.is_trading_day(run_date):
            log.debug("not a trading day, daily tier skipped",
                      extra={"tier": tier, "date": str(run_date)})
            return
        try:
            results = self.pipeline.run_categories(categories, force=True)
            errs = [r.error for r in results if r.error]
            status = "error" if errs else "ok"
            self.pipeline.dao.mark_job(job_id, run_date, status,
                                       "; ".join(errs))
        except Exception as exc:
            log.exception("daily tier crashed", extra={"tier": tier})
            self.pipeline.dao.mark_job(job_id, run_date, "error", str(exc))

    def _daily_report(self, run_date: date | None = None) -> None:
        d = run_date or self.cal.now().date()
        if not self.cal.is_trading_day(d):
            return
        try:
            report = DailyReport(self.cfg, self.pipeline.dao).generate(d)
            self.pipeline.dispatcher.send_report(
                report.title, report.summary, report.html_path
            )
            self.pipeline.dao.mark_job("report:daily", d, "ok")
        except Exception as exc:
            log.exception("daily report failed")
            self.pipeline.dao.mark_job("report:daily", d, "error", str(exc))

    def _weekly_report(self) -> None:
        d = self.cal.now().date()
        try:
            report = WeeklyReport(self.cfg, self.pipeline.dao).generate(d)
            self.pipeline.dispatcher.send_report(report.title, report.summary,
                                                 report.html_path)
            self.pipeline.dao.mark_job("report:weekly", d, "ok")
        except Exception:
            log.exception("weekly report failed")

    def _monthly_report(self) -> None:
        d = self.cal.now().date()
        # Fired on the 1st; the report covers the month that just ended.
        anchor = d - timedelta(days=1)
        try:
            report = MonthlyReport(self.cfg, self.pipeline.dao).generate(anchor)
            self.pipeline.dispatcher.send_report(report.title, report.summary,
                                                 report.html_path)
            self.pipeline.dao.mark_job("report:monthly", d, "ok")
        except Exception:
            log.exception("monthly report failed")

    def _health_sweep(self) -> None:
        problems = self.pipeline.check_health()
        if problems:
            log.warning("health problems detected",
                        extra={"count": len(problems)})

    def _nightly_maintenance(self) -> None:
        pruned = self.pipeline.prune()
        log.info("retention prune complete", extra=pruned)
        try:
            self.cal.load(refresh=True)
        except Exception as exc:
            log.warning("holiday refresh failed", extra={"err": str(exc)})

    # -- catch-up ------------------------------------------------------------
    def catch_up(self) -> None:
        cu = self.cfg.section("schedule.catchup")
        if not cu.get("enabled", True):
            return
        lookback = int(cu.get("max_lookback_days", 5))
        tiers = self._tier_map()
        today = self.cal.now().date()

        # Only backfill days that are fully done -- not the current session.
        candidates: list[date] = []
        d = today - timedelta(days=1)
        while len(candidates) < lookback and (today - d).days <= lookback * 3:
            if self.cal.is_trading_day(d):
                candidates.append(d)
            d -= timedelta(days=1)

        for run_date in reversed(candidates):
            for tier in ("daily", "daily_late"):
                cats = tiers.get(tier)
                if not cats:
                    continue
                if self.pipeline.dao.job_ran(f"tier:{tier}", run_date):
                    continue
                log.info("catching up missed tier",
                         extra={"tier": tier, "date": str(run_date)})
                # NSE serves the current state of these feeds, so a backfill
                # recovers the data but not the exact end-of-day snapshot.
                self._run_daily_categories(tier, cats, run_date)

            if not self.pipeline.dao.job_ran("report:daily", run_date):
                log.info("catching up missed daily report",
                         extra={"date": str(run_date)})
                self._daily_report(run_date)

    # -- wiring --------------------------------------------------------------
    def build(self) -> None:
        tiers = self._tier_map()
        sched_cfg = self.cfg.section("schedule.tiers")

        window_for = {"live_fast": "market_hours",
                      "live_slow": "market_hours",
                      "pre_open": "pre_open",
                      "session_close": "post_close"}

        for tier, categories in tiers.items():
            spec = sched_cfg.get(tier, {}) or {}
            if "interval_sec" in spec:
                self.sched.add_job(
                    self._run_tier, IntervalTrigger(
                        seconds=int(spec["interval_sec"])),
                    args=[tier, categories, window_for.get(tier)],
                    id=f"tier:{tier}", max_instances=1, coalesce=True,
                    misfire_grace_time=60,
                )
                log.info("scheduled interval tier",
                         extra={"tier": tier, "every_sec": spec["interval_sec"],
                                "categories": categories})
            elif "at" in spec:
                hh, mm = _hhmm(spec["at"], "18:30")
                self.sched.add_job(
                    self._run_daily_categories,
                    CronTrigger(day_of_week="mon-fri", hour=hh, minute=mm),
                    args=[tier, categories], id=f"tier:{tier}",
                    max_instances=1, misfire_grace_time=3600,
                )
                log.info("scheduled daily tier",
                         extra={"tier": tier, "at": spec["at"],
                                "categories": categories})

        # Reports. Daily runs after the last daily fetch tier.
        daily_at = sched_cfg.get("daily_late", {}).get("at", "19:45")
        hh, mm = _hhmm(daily_at, "19:45")
        report_minute = (mm + 15) % 60
        report_hour = hh + (1 if mm + 15 >= 60 else 0)
        self.sched.add_job(
            self._daily_report,
            CronTrigger(day_of_week="mon-fri", hour=report_hour,
                        minute=report_minute),
            id="report:daily", misfire_grace_time=7200,
        )

        weekly = sched_cfg.get("weekly", {}) or {}
        w_day = _DAY_ABBR.get(str(weekly.get("day", "sat")).lower(), 5)
        wh, wm = _hhmm(weekly.get("at"), "08:00")
        self.sched.add_job(
            self._weekly_report,
            CronTrigger(day_of_week=w_day, hour=wh, minute=wm),
            id="report:weekly", misfire_grace_time=7200,
        )

        monthly = sched_cfg.get("monthly", {}) or {}
        mh, mm2 = _hhmm(monthly.get("at"), "08:30")
        self.sched.add_job(
            self._monthly_report,
            CronTrigger(day=int(monthly.get("day", 1)), hour=mh, minute=mm2),
            id="report:monthly", misfire_grace_time=7200,
        )

        # Health sweep every 30 min, maintenance nightly.
        self.sched.add_job(self._health_sweep,
                           IntervalTrigger(minutes=30), id="health",
                           max_instances=1)
        self.sched.add_job(self._nightly_maintenance,
                           CronTrigger(hour=2, minute=15), id="maintenance")

    def start(self, block: bool = True) -> None:
        self.cal.load()
        log.info("trading calendar loaded",
                 extra={"holidays": len(self.cal.holidays),
                        "window_now": self.cal.window()})
        try:
            log.info("alert universe built",
                     extra={"symbols": len(self.pipeline.universe)})
        except Exception as exc:
            log.error("universe build failed", extra={"err": str(exc)})

        self.catch_up()
        self.build()
        self.sched.start()
        log.info("scheduler started",
                 extra={"jobs": [j.id for j in self.sched.get_jobs()]})

        if not block:
            return

        def _handle(_sig, _frame):
            log.info("shutdown signal received")
            self._stop.set()

        signal.signal(signal.SIGINT, _handle)
        try:
            signal.signal(signal.SIGTERM, _handle)
        except (AttributeError, ValueError):
            pass   # SIGTERM is not available on all Windows shells

        while not self._stop.is_set():
            self._stop.wait(1.0)
        self.stop()

    def stop(self) -> None:
        if self.sched.running:
            self.sched.shutdown(wait=False)
        log.info("scheduler stopped")
