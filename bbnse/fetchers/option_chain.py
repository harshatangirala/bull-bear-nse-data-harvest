"""Option chain.

Endpoint: /api/option-chain-v3?type=Indices&symbol=NIFTY

*** Units NOT verified against live data. ***
The endpoint returned an empty object on 2026-08-29 (a Saturday); NSE serves
this one only during and shortly after market hours. The shape below follows
the documented v3 response, and the fetcher is written defensively so an
unexpected payload yields zero rows rather than an exception.

This is why the accompanying rule is built entirely on the **put/call ratio**.
PCR is total PE open interest over total CE open interest -- both are the same
field on the same endpoint, so the ratio is dimensionless and stays correct
whatever unit `openInterest` turns out to be in. Absolute OI is carried
through for the reports but no threshold is applied to it until the unit has
been confirmed during market hours.

Re-verify with:  python main.py once option_chain --force   (during 09:15-15:30)
"""
from __future__ import annotations

from typing import Any

from .base import BaseFetcher, parse_nse_date, to_float, to_int


class OptionChainFetcher(BaseFetcher):
    category = "option_chain"
    endpoint_name = "option_chain"

    def payload_trade_date(self, payload: Any):
        if isinstance(payload, dict):
            records = payload.get("records") or {}
            return parse_nse_date(records.get("timestamp"))
        return None

    @staticmethod
    def _leg_rows(payload: Any) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        records = payload.get("records")
        if isinstance(records, dict) and isinstance(records.get("data"), list):
            return [r for r in records["data"] if isinstance(r, dict)]
        # Some responses are a bare list of strike rows.
        if isinstance(payload.get("data"), list):
            return [r for r in payload["data"] if isinstance(r, dict)]
        return []

    def normalize(self, payload: Any) -> list[dict]:
        rows = self._leg_rows(payload)
        if not rows:
            return []

        underlying = ""
        if isinstance(payload, dict):
            underlying = ((payload.get("records") or {}).get("underlying")
                          or self.endpoint.params.get("symbol") or "")

        ce_oi = pe_oi = 0
        ce_vol = pe_vol = 0
        strikes: list[dict] = []

        for row in rows:
            strike = to_float(row.get("strikePrice"))
            ce, pe = row.get("CE") or {}, row.get("PE") or {}
            ce_o = to_int(ce.get("openInterest")) or 0
            pe_o = to_int(pe.get("openInterest")) or 0
            ce_oi += ce_o
            pe_oi += pe_o
            ce_vol += to_int(ce.get("totalTradedVolume")) or 0
            pe_vol += to_int(pe.get("totalTradedVolume")) or 0
            strikes.append({"strike": strike, "ce_oi": ce_o, "pe_oi": pe_o})

        if not strikes:
            return []

        spot = None
        for row in rows:
            for leg in ("CE", "PE"):
                val = to_float((row.get(leg) or {}).get("underlyingValue"))
                if val:
                    spot = val
                    break
            if spot:
                break

        # Dimensionless, so it survives the unverified OI unit.
        pcr = (pe_oi / ce_oi) if ce_oi else None
        max_ce = max(strikes, key=lambda s: s["ce_oi"])
        max_pe = max(strikes, key=lambda s: s["pe_oi"])

        return [{
            "category": self.category,
            "bucket": "chain",
            "symbol": str(underlying).upper() or "NIFTY",
            "last_price": spot,
            "extra": {
                "pcr": pcr,
                "total_ce_oi": ce_oi,
                "total_pe_oi": pe_oi,
                "total_ce_volume": ce_vol,
                "total_pe_volume": pe_vol,
                "strike_count": len(strikes),
                # Highest-OI strikes are the market's implied support and
                # resistance; carried for the reports.
                "max_ce_oi_strike": max_ce["strike"],
                "max_pe_oi_strike": max_pe["strike"],
                "oi_unit": "UNVERIFIED",
            },
        }]
