"""Shared test helpers.

Every test module builds its config from `make_cfg` so that a change to the
default rule set lands in one place rather than fifteen. Phase-2 modules add
their own `rules.<category>` blocks via the overrides argument.
"""
from __future__ import annotations

from typing import Any

from bbnse.core.config import Config
from bbnse.storage.dao import Dao

BASE: dict[str, Any] = {
    "app": {"timezone": "Asia/Kolkata"},
    "rules": {
        "debounce": {
            "remind_after_hours": 3, "max_reminders": 1,
            "escalate_on_pct_move": 5.0, "close_after_missed_polls": 2,
            "reset_intraday_at_open": True,
            "max_alerts_per_symbol_per_day": 0,
        },
        "cross_feed_dedup": {
            "enabled": True, "window_minutes": 30,
            "escalate_on_higher_severity": True,
            "escalate_on_higher_priority": True,
            "groups": {
                "deals": ["large_deals"],
                "equity_move": ["gainers", "losers", "volume_spurts",
                                "price_band_hitters", "most_active_value"],
                "derivatives": ["oi_spurts", "derivatives_watch",
                                "most_active_contracts", "option_chain"],
            },
        },
        "gainers_losers": {
            "enabled": True, "pct_move_notable": 5.0, "pct_move_critical": 9.0,
            "min_traded_value_cr": 5.0,
            "buckets": ["NIFTY", "BANKNIFTY", "NIFTYNEXT50", "FOSec", "allSec"],
        },
        "week52": {
            "enabled": True, "min_ltp": 20.0, "min_margin_pct": 0.0,
            "breakout_margin_pct_critical": 3.0,
            "severity_high": "notable", "severity_low": "notable",
        },
        "large_deals": {
            "enabled": True, "value_cr_notable": 10.0,
            "value_cr_critical": 50.0, "block_value_cr_notable": 25.0,
            "deal_types": ["BULK", "BLOCK", "SHORT"],
            "watch_clients": [], "respect_universe": False,
        },
        "volume_spurts": {
            "enabled": True, "volume_vs_week1_avg_multiple": 3.0,
            "volume_vs_week2_avg_multiple": 3.0, "sigma_above_mean": 2.5,
            "min_traded_value_cr": 2.0, "critical_multiple": 6.0,
        },
        "oi_spurts": {
            "enabled": True, "oi_change_pct_notable": 20.0,
            "oi_change_pct_critical": 40.0, "min_oi_contracts": 5000,
        },
        "price_band": {
            "enabled": True, "alert_on": ["upper", "lower"],
            "severity": "critical", "min_ltp": 20.0,
        },
        "advance_decline": {
            "enabled": True, "ratio_extreme_bullish": 3.0,
            "ratio_extreme_bearish": 0.33, "min_total": 100,
        },
        "new_listings": {"enabled": True, "severity": "notable"},
        "most_active": {
            "enabled": True, "min_traded_value_cr": 100.0,
            "critical_value_cr": 500.0,
        },
        "indices": {
            "enabled": True, "pct_move_notable": 1.0,
            "pct_move_critical": 2.0,
            "watch": ["NIFTY 50", "NIFTY BANK"],
        },
        "pre_open": {
            "enabled": True, "pct_move_notable": 4.0,
            "pct_move_critical": 8.0, "min_value_cr": 1.0,
            "persist_min_pct_move": 2.0,
        },
        "etf": {"enabled": True, "premium_discount_pct": 1.5,
                "min_traded_value_cr": 1.0},
        "derivatives_watch": {"enabled": True, "pct_move_notable": 3.0,
                              "pct_move_critical": 6.0},
        "most_active_contracts": {
            "enabled": True, "pct_move_notable": 25.0,
            "pct_move_critical": 50.0, "top_n_by_turnover": 10,
            "min_open_interest": 0,
        },
        "option_chain": {"enabled": True, "pcr_bullish": 1.5,
                         "pcr_bearish": 0.6, "min_total_oi": 100000},
        "fii_dii": {"enabled": True, "net_cr_notable": 2000.0,
                    "net_cr_critical": 5000.0},
        "daily_reports": {"enabled": True, "severity": "info"},
        "gsm": {"enabled": True, "severity": "notable"},
        "asm": {"enabled": True, "severity": "notable",
               "terms": ["longterm", "shortterm"]},
        "surveillance_price_bands": {"enabled": True, "severity": "critical"},
        "slb": {"enabled": True, "spread_pct_notable": 3.0,
               "spread_pct_critical": 6.0, "yield_pct_notable": 8.0},
        "closing_auction": {"enabled": True, "pct_move_notable": 5.0,
                            "pct_move_critical": 10.0},
    },
    "notifiers": {"console": {"enabled": False}},
    "storage": {"keep_raw_snapshots": True},
}


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def make_cfg(**overrides) -> Config:
    """Config built from BASE, with dotted-path overrides applied.

        make_cfg(**{"rules.week52.min_ltp": 100.0})
    """
    import copy
    data = copy.deepcopy(BASE)
    for dotted, value in overrides.items():
        node = data
        parts = dotted.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        if isinstance(value, dict) and isinstance(node.get(parts[-1]), dict):
            node[parts[-1]] = _deep_merge(node[parts[-1]], value)
        else:
            node[parts[-1]] = value
    return Config(data, path=None)  # type: ignore[arg-type]


def make_dao() -> Dao:
    d = Dao("sqlite:///:memory:")
    d.create_all()
    return d


class FakeUniverse:
    """Universe stub: only the listed symbols are in scope."""

    def __init__(self, *symbols: str):
        self.symbols = {s.upper() for s in symbols}

    def __contains__(self, symbol: str) -> bool:
        return (symbol or "").upper() in self.symbols

    def __len__(self) -> int:
        return len(self.symbols)
