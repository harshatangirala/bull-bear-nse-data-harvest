"""Securities Lending & Borrowing.

Endpoint: /api/live-analysis-slb?series=<key>

The `series` parameter is not a fixed value -- it is a rolling monthly code
("10" this poll, "11" next month) that the frontend re-derives every load
from `/api/live-analysis-slb-series-master`'s `filter.series.key` field. This
fetcher does the same: it polls the series-master first, uses its `filter`
key as the "current" series, and only falls back to the config default if
that call fails.

Units verified 2026-09-01 against a live sample: for IREDA,
underLyingLtp=115.05, futuresLtp=111.2, spread=3.85 (== 115.05-111.2 exactly)
and spreadPer=3.35 (== spread/underLyingLtp*100 = 3.347, matching to the
reported precision). `spreadPer` is therefore a plain percent, dimensionless.

`turnOver` and `transactionValue` could NOT be unit-verified: every row in
the live sample had zero volume (no SLB trades in the current series at
poll time), so there was nothing to cross-check arithmetically. They are
carried through unconverted and explicitly unused by the importance rule --
see processors/slb.py.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .base import BaseFetcher, parse_nse_date, to_float, to_int

_FALLBACK_SERIES = "10"


class SlbFetcher(BaseFetcher):
    category = "slb"
    endpoint_name = "slb"

    def __init__(self, cfg, session, dao):
        super().__init__(cfg, session, dao)
        self._current_series = _FALLBACK_SERIES

    def payload_trade_date(self, payload: Any) -> date | None:
        if isinstance(payload, dict):
            return parse_nse_date(payload.get("timestamp"))
        return None

    def fetch(self) -> Any:
        # Refresh the current series key before every poll -- it changes once
        # a month, and using a stale key silently returns the wrong month's
        # (usually empty, since expired months quiet down) book.
        try:
            master_ep = self.registry.get("slb_series_master")
            self.session.warm(master_ep.referer)
            res = self.session.get_json(master_ep.full_url,
                                        referer=master_ep.referer)
            key = ((res.json or {}).get("filter") or {}).get("series", {}).get("key")
            if key:
                self._current_series = key
        except Exception:
            # Non-fatal: fall back to the last known-good series key.
            pass

        ep = self.endpoint
        self.session.warm(ep.referer)
        res = self.session.get_json(ep.full_url, referer=ep.referer,
                                    params={"series": self._current_series},
                                    timeout=ep.timeout)
        if not res.ok:
            from ..core.session import NSEFetchError
            raise NSEFetchError(f"{ep.name}: HTTP {res.status}")
        return res.json

    def normalize(self, payload: Any) -> list[dict]:
        out: list[dict] = []
        for row in self.rows_from(payload):
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            spread = to_float(row.get("spread"))
            spread_pct = to_float(row.get("spreadPer"))
            out.append({
                "category": self.category,
                "bucket": "slb",
                "symbol": symbol,
                "last_price": to_float(row.get("underLyingLtp")),
                "volume": to_int(row.get("volume")),
                "extra": {
                    "series": getattr(self, "_current_series", _FALLBACK_SERIES),
                    "futures_ltp": to_float(row.get("futuresLtp")),
                    "spread": spread,
                    "spread_pct": spread_pct,     # verified dimensionless
                    "open_positions": to_int(row.get("openPositions")),
                    "annualised_yield_pct": to_float(
                        row.get("annualisedYieldPer")),
                    # Unverified units -- not used by the importance rule.
                    "turnover_unverified": to_float(row.get("turnOver")),
                    "transaction_value_unverified": to_float(
                        row.get("transactionValue")),
                    "ca": row.get("ca") or "",
                    "ca_exp_date": row.get("caExpDate") or "",
                },
            })
        return out
