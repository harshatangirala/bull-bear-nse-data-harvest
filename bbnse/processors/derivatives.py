"""Importance rules for the derivatives feeds.

Four rules share this module because they share a correlation group: an OI
surge in a name usually shows up in the derivatives watch and the most-active
list in the same session, and the deduplicator collapses them.

A note on `most_active_contracts`: the turnover fields on that endpoint could
not be unit-verified (see fetchers/derivatives.py), so its rule uses only
`pChange` (a percent) and `openInterest` (contracts). Turnover is used purely
as NSE's own ordering, never as an amount.
"""
from __future__ import annotations

from .base import BaseProcessor, Signal
from .correlate import make_dedup_key


class OiSpurtsProcessor(BaseProcessor):
    category = "oi_spurts"
    config_key = "oi_spurts"
    rule_id = "oi_change"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.notable = float(self.rules.get("oi_change_pct_notable", 20.0))
        self.critical = float(self.rules.get("oi_change_pct_critical", 40.0))
        self.min_oi = int(self.rules.get("min_oi_contracts", 0))

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            symbol = row.get("symbol") or ""
            if not self.in_universe(symbol):
                continue

            extra = row.get("extra") or {}
            # NSE ships this as "avgInOI"; the fetcher renames it because it
            # is a percent change, not an average.
            pct = extra.get("oi_change_pct")
            latest_oi = extra.get("latest_oi") or 0
            if pct is None:
                continue

            magnitude = abs(pct)
            if magnitude < self.notable:
                continue
            # A 300% jump from 12 contracts to 48 is not a signal.
            if self.min_oi > 0 and latest_oi < self.min_oi:
                continue

            severity = "critical" if magnitude >= self.critical else "notable"
            building = pct > 0
            arrow = "▲" if building else "▼"
            verb = "OI BUILD-UP" if building else "OI UNWIND"

            body_bits = [f"OI {extra.get('prev_oi'):,} -> {latest_oi:,} contracts"
                         if extra.get("prev_oi") is not None else ""]
            if extra.get("fut_value_cr") is not None:
                body_bits.append(f"futures Rs {extra['fut_value_cr']:,.0f} cr")
            if extra.get("underlying_value") is not None:
                body_bits.append(f"spot {extra['underlying_value']:,.2f}")

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=symbol,
                state_bucket=f"oi_{'build' if building else 'unwind'}",
                severity=severity,
                title=f"{arrow} {verb} {symbol} {pct:+.1f}%",
                body=" | ".join(b for b in body_bits if b),
                value=pct,
                dedup_key=make_dedup_key("derivatives", symbol),
                dedup_priority=2,      # the most specific derivatives signal
                payload={"symbol": symbol, "oi_change_pct": pct,
                         "latest_oi": latest_oi,
                         "prev_oi": extra.get("prev_oi"),
                         "change_in_oi": extra.get("change_in_oi"),
                         "fut_value_cr": extra.get("fut_value_cr")},
            ))
        return signals


class DerivativesWatchProcessor(BaseProcessor):
    category = "derivatives_watch"
    config_key = "derivatives_watch"
    rule_id = "contract_move"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.notable = float(self.rules.get("pct_move_notable", 3.0))
        self.critical = float(self.rules.get("pct_move_critical", 6.0))

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            pct = row.get("pct_change")
            if pct is None:
                continue
            magnitude = abs(pct)
            if magnitude < self.notable:
                continue

            extra = row.get("extra") or {}
            underlying = (extra.get("underlying") or "").upper()
            # Index futures have no equity symbol, so the universe check is
            # applied to the underlying only when there is one to check.
            if underlying and self.universe is not None:
                if underlying not in self.universe and not underlying.startswith(
                        ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCP")):
                    continue

            severity = "critical" if magnitude >= self.critical else "notable"
            arrow = "▲" if pct > 0 else "▼"
            contract = row.get("symbol") or ""

            body_bits = []
            if row.get("last_price") is not None:
                body_bits.append(f"LTP {row['last_price']:,.2f}")
            if row.get("traded_value") is not None:
                body_bits.append(f"turnover Rs {row['traded_value']:,.0f} cr")
            if extra.get("instrument"):
                body_bits.append(extra["instrument"])

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=contract,
                state_bucket=f"contract_{'up' if pct > 0 else 'down'}",
                severity=severity,
                title=f"{arrow} {contract} {pct:+.2f}%",
                body=" | ".join(body_bits),
                value=pct,
                dedup_key=make_dedup_key("derivatives", underlying or contract),
                dedup_priority=1,
                payload={"contract": contract, "underlying": underlying,
                         "pct_change": pct, "ltp": row.get("last_price")},
            ))
        return signals


class MostActiveContractsProcessor(BaseProcessor):
    category = "most_active_contracts"
    config_key = "most_active_contracts"
    rule_id = "active_contract_move"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.notable = float(self.rules.get("pct_move_notable", 25.0))
        self.critical = float(self.rules.get("pct_move_critical", 50.0))
        self.top_n = int(self.rules.get("top_n_by_turnover", 10))
        self.min_oi = int(self.rules.get("min_open_interest", 0))

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        seen: set[str] = set()
        for row in rows:
            extra = row.get("extra") or {}
            # Turnover units are unverified on this endpoint, so it is used
            # only as NSE's own ranking, never compared to a rupee threshold.
            if extra.get("rank", 999) > self.top_n:
                continue

            pct = row.get("pct_change")
            if pct is None or abs(pct) < self.notable:
                continue
            oi = extra.get("open_interest") or 0
            if self.min_oi > 0 and oi < self.min_oi:
                continue

            contract = row.get("symbol") or ""
            if contract in seen:      # appears in both volume and value lists
                continue
            seen.add(contract)

            magnitude = abs(pct)
            severity = "critical" if magnitude >= self.critical else "notable"
            arrow = "▲" if pct > 0 else "▼"
            underlying = (extra.get("underlying") or "").upper()

            body_bits = [f"rank #{extra.get('rank')} by {extra.get('ranked_by')}"]
            if row.get("last_price") is not None:
                body_bits.append(f"LTP {row['last_price']:,.2f}")
            if oi:
                body_bits.append(f"OI {oi:,} contracts")
            if extra.get("strike"):
                body_bits.append(f"strike {extra['strike']:,.0f}")

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=contract,
                state_bucket=f"active_{'up' if pct > 0 else 'down'}",
                severity=severity,
                title=f"{arrow} ACTIVE CONTRACT {contract} {pct:+.1f}%",
                body=" | ".join(body_bits),
                value=pct,
                dedup_key=make_dedup_key("derivatives", underlying or contract),
                dedup_priority=0,
                payload={"contract": contract, "underlying": underlying,
                         "pct_change": pct, "open_interest": oi,
                         "rank": extra.get("rank")},
            ))
        return signals


class OptionChainProcessor(BaseProcessor):
    category = "option_chain"
    config_key = "option_chain"
    rule_id = "pcr_extreme"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.bullish = float(self.rules.get("pcr_bullish", 1.5))
        self.bearish = float(self.rules.get("pcr_bearish", 0.6))
        self.min_total_oi = int(self.rules.get("min_total_oi", 0))

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            extra = row.get("extra") or {}
            pcr = extra.get("pcr")
            if pcr is None:
                continue

            total_oi = (extra.get("total_ce_oi") or 0) + (extra.get("total_pe_oi") or 0)
            if self.min_total_oi > 0 and total_oi < self.min_total_oi:
                continue

            # PCR is a ratio of two values from the same field, so it holds
            # regardless of what unit openInterest turns out to be in.
            if pcr >= self.bullish:
                direction, arrow = "bullish", "▲"
            elif pcr <= self.bearish:
                direction, arrow = "bearish", "▼"
            else:
                continue

            symbol = row.get("symbol") or "NIFTY"
            body_bits = [
                f"PE OI {extra.get('total_pe_oi'):,} vs CE OI "
                f"{extra.get('total_ce_oi'):,}",
            ]
            if extra.get("max_pe_oi_strike"):
                body_bits.append(f"support {extra['max_pe_oi_strike']:,.0f}")
            if extra.get("max_ce_oi_strike"):
                body_bits.append(f"resistance {extra['max_ce_oi_strike']:,.0f}")
            if row.get("last_price"):
                body_bits.append(f"spot {row['last_price']:,.2f}")

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=symbol,
                state_bucket=f"pcr_{direction}",
                severity="notable",
                title=f"{arrow} PCR {direction} {symbol} at {pcr:.2f}",
                body=" | ".join(body_bits),
                value=pcr,
                payload={"symbol": symbol, "pcr": pcr,
                         "total_ce_oi": extra.get("total_ce_oi"),
                         "total_pe_oi": extra.get("total_pe_oi"),
                         "max_pe_oi_strike": extra.get("max_pe_oi_strike"),
                         "max_ce_oi_strike": extra.get("max_ce_oi_strike")},
            ))
        return signals
