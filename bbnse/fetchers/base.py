"""Fetcher contract.

A fetcher does exactly four things: pull the endpoint, store the raw payload,
normalize rows into a stable internal shape, and persist those rows. It never
decides whether anything is important -- that is the processors' job. Keeping
the split clean is what lets reports be regenerated from history later.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..core.logging_setup import get_logger
from ..core.registry import Endpoint, extract_rows, load_registry
from ..core.session import NSEFetchError, NSESession
from ..storage.dao import Dao

log = get_logger(__name__)

_DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d-%m-%Y")
_IST = ZoneInfo("Asia/Kolkata")


def _today_ist() -> date:
    """Fallback trade_date for payloads that carry no date field of their own.

    Used only when payload_trade_date() returns None -- most categories
    (price_band_hitters, most_active_contracts' outer shape, and any future
    fetcher that forgets to override it) have no per-row timestamp, so
    without this every observation row from them lands with trade_date=NULL
    and silently drops out of every report and retention query that filters
    on trade_date.
    """
    return datetime.now(_IST).date()


def parse_nse_date(raw: Any) -> date | None:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    # Values like "28-Aug-2026 15:30:00" appear in some feeds.
    text = text.split(" ")[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def to_float(raw: Any) -> float | None:
    """NSE mixes numbers and comma-formatted strings in the same field."""
    if raw is None or raw == "-":
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).replace(",", "").strip()
    if not text or text in {"-", "NA", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def to_int(raw: Any) -> int | None:
    val = to_float(raw)
    return int(val) if val is not None else None


# ---------------------------------------------------------------------------
# Unit conversion.
#
# NSE is NOT consistent about money units between endpoints. Every converter
# below corresponds to a convention verified arithmetically against a live
# sample of that specific endpoint -- see docs/endpoints.md > Unit conventions.
# Do not reuse one without re-checking, however similar the field name looks:
#   gainers.turnover              is LAKH
#   most_active.totalTradedValue  is RUPEES
#   price_band.turnover           is CRORE
# ...all three are "the value traded".
#
# Everything in config.yaml is expressed in CRORE, so all of these land there.
# ---------------------------------------------------------------------------

def lakh_to_cr(raw: Any) -> float | None:
    """LAKH -> crore. (gainers/losers, volume_spurts, oi_spurts fut/prem)"""
    val = to_float(raw)
    return val / 100.0 if val is not None else None


def rupees_to_cr(raw: Any) -> float | None:
    """RUPEES -> crore. (most_active_value, etf, derivatives_watch, pre_open)"""
    val = to_float(raw)
    return val / 1e7 if val is not None else None


def cr_to_cr(raw: Any) -> float | None:
    """Already CRORE. (price_band_hitters, advance_decline, fii_dii)

    Exists so every fetcher states its unit explicitly at the call site
    rather than silently passing a number through.
    """
    return to_float(raw)


def lakh_shares_to_shares(raw: Any) -> int | None:
    """LAKH shares -> share count. (price_band_hitters, advance_decline)"""
    val = to_float(raw)
    return int(val * 1e5) if val is not None else None


@dataclass
class FetchOutcome:
    category: str
    rows: list[dict] = field(default_factory=list)
    raw: Any = None
    snapshot_id: int | None = None
    payload_changed: bool = True
    elapsed_sec: float = 0.0
    trade_date: date | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def row_count(self) -> int:
        return len(self.rows)


class BaseFetcher:
    """Subclasses set `category` / `endpoint_name` and implement normalize()."""

    category: str = ""
    endpoint_name: str = ""
    # Observation rows go to the `observation` table unless overridden.
    persists_observations: bool = True

    def __init__(self, cfg, session: NSESession, dao: Dao):
        self.cfg = cfg
        self.session = session
        self.dao = dao
        self.registry = load_registry()

    @property
    def endpoint(self) -> Endpoint:
        return self.registry.get(self.endpoint_name or self.category)

    # -- overridable ---------------------------------------------------------
    def normalize(self, payload: Any) -> list[dict]:
        """Map NSE's payload onto Observation-shaped dicts."""
        raise NotImplementedError

    def payload_trade_date(self, payload: Any) -> date | None:
        return None

    def rows_from(self, payload: Any, root: str | None = None) -> list[dict]:
        return extract_rows(payload, root if root is not None
                            else self.endpoint.row_root)

    # -- pipeline ------------------------------------------------------------
    def fetch(self) -> Any:
        ep = self.endpoint
        if not ep.resolved:
            raise NSEFetchError(
                f"endpoint '{ep.name}' is UNRESOLVED (page: {ep.page})"
            )
        # Visiting the landing page once makes the Referer legitimate.
        self.session.warm(ep.referer)
        res = self.session.get_json(ep.full_url, referer=ep.referer,
                                    params=ep.params or None,
                                    timeout=ep.timeout)
        if not res.ok:
            raise NSEFetchError(
                f"{ep.name}: HTTP {res.status} from {res.url}"
            )
        return res.json

    def persist(self, rows: list[dict], trade_date: date | None) -> None:
        if not (self.persists_observations and rows):
            return
        payload = []
        for r in rows:
            rec = dict(r)
            rec.setdefault("category", self.category)
            rec.setdefault("trade_date", trade_date)
            payload.append(rec)
        self.dao.add_observations(payload)

    def run(self) -> FetchOutcome:
        started = time.time()
        try:
            payload = self.fetch()
        except Exception as exc:
            log.error("fetch failed", extra={"category": self.category,
                                             "err": str(exc)})
            return FetchOutcome(category=self.category, error=str(exc),
                                elapsed_sec=time.time() - started)

        trade_date = self.payload_trade_date(payload) or _today_ist()
        try:
            rows = self.normalize(payload)
        except Exception as exc:
            log.exception("normalize failed",
                          extra={"category": self.category})
            return FetchOutcome(category=self.category,
                                error=f"normalize: {exc}", raw=payload,
                                elapsed_sec=time.time() - started)

        snapshot_id, was_new = (None, True)
        if self.cfg.get("storage.keep_raw_snapshots", True):
            snapshot_id, was_new = self.dao.store_snapshot(
                self.category, payload, url=self.endpoint.full_url,
                row_count=len(rows), trade_date=trade_date,
            )

        # Only write observations when the market state actually moved. A live
        # category polled every 2 minutes for 6 hours is ~180 polls; storing
        # identical rows each time would add ~100k rows/day across categories
        # for no analytical gain. One row per distinct state keeps the series
        # meaningful and the DB small.
        if was_new:
            self.persist(rows, trade_date)
        elapsed = time.time() - started
        log.info("fetched", extra={"category": self.category, "rows": len(rows),
                                   "changed": was_new,
                                   "elapsed": round(elapsed, 2)})
        return FetchOutcome(category=self.category, rows=rows, raw=payload,
                            snapshot_id=snapshot_id, payload_changed=was_new,
                            elapsed_sec=elapsed, trade_date=trade_date)
