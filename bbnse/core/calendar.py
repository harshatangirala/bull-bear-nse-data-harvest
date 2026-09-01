"""Trading calendar.

Holidays come from NSE's own /api/holiday-master rather than a hardcoded
list, so the schedule stays correct across years without maintenance. The
result is cached to disk; if NSE is unreachable at startup we fall back to
the cache and, failing that, to weekends-only (fail open -- a wasted poll on
a holiday is cheaper than missing a whole trading day).
"""
from __future__ import annotations

import json
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import PROJECT_ROOT
from .logging_setup import get_logger
from .registry import load_registry

log = get_logger(__name__)

CACHE = PROJECT_ROOT / "data" / "holidays.cache.json"
_DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d")


def _parse_nse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


class MarketCalendar:
    def __init__(self, cfg, session=None):
        self.cfg = cfg
        self.session = session
        self.tz = ZoneInfo(cfg.get("app.timezone", "Asia/Kolkata"))
        m = cfg.section("schedule.market")
        self.pre_open_start = self._t(m.get("pre_open_start", "09:00"))
        self.open = self._t(m.get("open", "09:15"))
        self.close = self._t(m.get("close", "15:30"))
        self.post_close_end = self._t(m.get("post_close_end", "16:00"))
        self._holidays: set[date] = set()
        self._loaded = False

    @staticmethod
    def _t(hhmm: str) -> dtime:
        hh, mm = str(hhmm).split(":")[:2]
        return dtime(int(hh), int(mm))

    # -- holiday sourcing ----------------------------------------------------
    def load(self, refresh: bool = False) -> None:
        if self._loaded and not refresh:
            return
        if not refresh and self._load_cache():
            self._loaded = True
            return
        if self.session is not None:
            try:
                self._fetch_remote()
                self._loaded = True
                return
            except Exception as exc:
                log.warning("holiday fetch failed, falling back",
                            extra={"err": str(exc)})
        if self._load_cache():
            self._loaded = True
            return
        log.warning("no holiday data; treating weekends as the only closures")
        self._loaded = True

    def _load_cache(self) -> bool:
        if not CACHE.exists():
            return False
        try:
            doc = json.loads(CACHE.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(doc["fetched_at"]).date()
            # Refresh at least quarterly; NSE publishes the list yearly.
            if (date.today() - fetched).days > 90:
                return False
            self._holidays = {date.fromisoformat(d) for d in doc["holidays"]}
            log.debug("holidays from cache", extra={"count": len(self._holidays)})
            return True
        except Exception:
            return False

    def _fetch_remote(self) -> None:
        ep = load_registry().get("holiday_master")
        res = self.session.get_json(ep.full_url, referer=ep.referer,
                                    params=ep.params)
        payload = res.json or {}
        found: set[date] = set()
        # Payload is keyed by segment (CBM = capital market, CD, FO, ...).
        # CBM is the equity calendar we care about.
        for segment in ("CBM", "CM", "FO"):
            for row in payload.get(segment, []) or []:
                d = _parse_nse_date(row.get("tradingDate", ""))
                if d:
                    found.add(d)
            if found:
                break
        if not found:
            raise ValueError("holiday-master returned no parseable dates")
        self._holidays = found
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({
            "fetched_at": datetime.now().isoformat(),
            "holidays": sorted(d.isoformat() for d in found),
        }, indent=1), encoding="utf-8")
        log.info("holidays refreshed from NSE", extra={"count": len(found)})

    # -- queries -------------------------------------------------------------
    @property
    def holidays(self) -> set[date]:
        self.load()
        return self._holidays

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def is_trading_day(self, d: date | None = None) -> bool:
        d = d or self.now().date()
        if d.weekday() >= 5:            # Sat/Sun
            return False
        return d not in self.holidays

    def previous_trading_day(self, d: date | None = None) -> date:
        d = (d or self.now().date()) - timedelta(days=1)
        for _ in range(15):
            if self.is_trading_day(d):
                return d
            d -= timedelta(days=1)
        return d

    def window(self, at: datetime | None = None) -> str:
        """Which schedule window we are in: closed|pre_open|market_hours|post_close."""
        at = at or self.now()
        if not self.is_trading_day(at.date()):
            return "closed"
        t = at.time()
        if self.pre_open_start <= t < self.open:
            return "pre_open"
        if self.open <= t <= self.close:
            return "market_hours"
        if self.close < t <= self.post_close_end:
            return "post_close"
        return "closed"

    def is_open(self, at: datetime | None = None) -> bool:
        return self.window(at) == "market_hours"

    def session_start(self, d: date | None = None) -> datetime:
        d = d or self.now().date()
        return datetime.combine(d, self.open, tzinfo=self.tz)
