"""Derivatives market watch and most-active contracts.

Endpoints: /api/liveEquity-derivatives?index=nse50_fut
           /api/snapshot-derivatives-equity?index=contracts&limit=20

Units verified 2026-08-29 against live samples:

derivatives_watch
  volume            UNITS, not contracts. volume x lastPrice reproduced
                    totalTurnover to within 0.2% on all three rows
                    (NIFTY Sep future: 2,163,135 x 24,349 = 52.67e9 against a
                    reported 52.59e9).
  totalTurnover     RUPEES
  value             RUPEES, identical to totalTurnover (ratio exactly 1.0)

most_active_contracts
  numberOfContractsTraded   contracts
  openInterest              contracts
  pChange                   percent
  totalTurnover             *** UNVERIFIED ***
  premiumTurnover           *** UNVERIFIED ***
      No consistent relationship survives across rows: premiumTurnover /
      totalTurnover ranged 253x to 763x over five contracts, and neither
      contracts x price nor contracts x strike reconciles with either field
      under any single unit assumption. Rather than guess, the processor for
      this category uses only the fields above whose units are established,
      and treats turnover as an ordering key rather than an absolute amount.
      Re-check during market hours -- see docs/endpoints.md.
"""
from __future__ import annotations

from typing import Any

from .base import BaseFetcher, parse_nse_date, rupees_to_cr, to_float, to_int


class DerivativesWatchFetcher(BaseFetcher):
    category = "derivatives_watch"
    endpoint_name = "derivatives_watch"

    def payload_trade_date(self, payload):
        if isinstance(payload, dict):
            return parse_nse_date(payload.get("timestamp"))
        return None

    def normalize(self, payload: Any) -> list[dict]:
        out: list[dict] = []
        for row in self.rows_from(payload):
            contract = (row.get("contract") or row.get("identifier") or "").strip()
            if not contract:
                continue
            out.append({
                "category": self.category,
                "bucket": row.get("instrumentType") or "",
                # The contract is the entity, not the underlying: two expiries
                # of the same underlying are different instruments.
                "symbol": contract.upper()[:32],
                "company": row.get("underlying") or "",
                "last_price": to_float(row.get("lastPrice")),
                "prev_close": to_float(row.get("closePrice")),
                "change": to_float(row.get("change")),
                "pct_change": to_float(row.get("pChange")),
                "volume": to_int(row.get("volume")),          # UNITS
                "traded_value": rupees_to_cr(row.get("totalTurnover")),
                "extra": {
                    "underlying": row.get("underlying"),
                    "instrument": row.get("instrument"),
                    "expiry": str(parse_nse_date(row.get("expiryDate")) or ""),
                    "option_type": row.get("optionType"),
                    "strike": to_float(row.get("strikePrice")),
                    "open": to_float(row.get("openPrice")),
                    "high": to_float(row.get("highPrice")),
                    "low": to_float(row.get("lowPrice")),
                },
            })
        return out


class MostActiveContractsFetcher(BaseFetcher):
    category = "most_active_contracts"
    endpoint_name = "most_active_contracts"

    def payload_trade_date(self, payload):
        # The timestamp sits inside each ranking sub-object, not at the top.
        if isinstance(payload, dict):
            for bucket in ("volume", "value"):
                ts = (payload.get(bucket) or {}).get("timestamp")
                parsed = parse_nse_date(ts)
                if parsed:
                    return parsed
        return None

    def normalize(self, payload: Any) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        out: list[dict] = []
        # Two rankings ship in one payload; volume is the one we key on.
        for bucket in ("volume", "value"):
            node = payload.get(bucket)
            rows = (node or {}).get("data") or []
            for rank, row in enumerate(rows, start=1):
                if not isinstance(row, dict):
                    continue
                ident = (row.get("identifier") or "").strip()
                if not ident:
                    continue
                out.append({
                    "category": self.category,
                    "bucket": bucket,
                    "symbol": ident.upper()[:32],
                    "company": row.get("underlying") or "",
                    "last_price": to_float(row.get("lastPrice")),
                    "pct_change": to_float(row.get("pChange")),
                    "volume": to_int(row.get("numberOfContractsTraded")),
                    # Deliberately NOT populating traded_value: the unit of
                    # totalTurnover on this endpoint could not be verified.
                    "traded_value": None,
                    "extra": {
                        "rank": rank,
                        "ranked_by": bucket,
                        "underlying": row.get("underlying"),
                        "instrument": row.get("instrument"),
                        "option_type": row.get("optionType"),
                        "strike": to_float(row.get("strikePrice")),
                        "expiry": str(parse_nse_date(row.get("expiryDate")) or ""),
                        "open_interest": to_int(row.get("openInterest")),
                        "underlying_value": to_float(row.get("underlyingValue")),
                        # Raw, unconverted, explicitly labelled as unverified.
                        "raw_total_turnover_unverified":
                            to_float(row.get("totalTurnover")),
                        "raw_premium_turnover_unverified":
                            to_float(row.get("premiumTurnover")),
                    },
                })
        return out
