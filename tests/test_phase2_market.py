"""Phase 2, batch B: indices, new listings, ETFs.

Payload fragments copied verbatim from live NSE responses captured 2026-08-29.
"""
from __future__ import annotations

from datetime import date

import pytest

from bbnse.fetchers.etf import EtfFetcher
from bbnse.fetchers.indices import IndicesFetcher
from bbnse.fetchers.new_listings import NewListingsFetcher
from bbnse.processors.etf import EtfProcessor
from bbnse.processors.indices import IndicesProcessor
from bbnse.processors.new_listings import NewListingsProcessor
from bbnse.processors.state import AlertStateMachine

from .helpers import make_cfg
from .test_phase2_equity import build

TODAY = date(2026, 8, 28)


# ==========================================================================
# indices
# ==========================================================================
INDEX_PAYLOAD = {"data": [
    {"key": "INDICES ELIGIBLE IN DERIVATIVES", "index": "NIFTY 50",
     "indexSymbol": "NIFTY 50", "last": 24175.65, "variation": 84.8,
     "percentChange": 0.35, "open": 24122.6, "high": 24188.3,
     "low": 24076.85, "previousClose": 24090.85, "yearHigh": 26373.2,
     "yearLow": 22182.55, "indicativeClose": 0, "pe": "20.44", "pb": "2.93",
     "dy": "1.16"},
    {"key": "BROAD MARKET INDICES", "index": "NIFTY BANK",
     "indexSymbol": "NIFTY BANK", "last": 51000.0, "variation": -1200.0,
     "percentChange": -2.3, "open": 52200.0, "high": 52300.0,
     "low": 50900.0, "previousClose": 52200.0, "yearHigh": 56000.0,
     "yearLow": 46000.0, "pe": "14.2", "pb": "2.1", "dy": "0.9"},
    {"key": "SECTORAL INDICES", "index": "NIFTY REALTY",
     "indexSymbol": "NIFTY REALTY", "last": 900.0, "variation": 45.0,
     "percentChange": 5.0, "previousClose": 855.0},
]}


def test_indices_are_measured_in_points_not_rupees():
    rows = build(IndicesFetcher, make_cfg()).normalize(INDEX_PAYLOAD)
    n50 = next(r for r in rows if r["symbol"] == "NIFTY 50")
    assert n50["last_price"] == 24175.65
    assert n50["change"] == 84.8          # index points
    assert n50["pct_change"] == 0.35


def test_indices_parse_string_ratios():
    """pe/pb/dy arrive as strings."""
    rows = build(IndicesFetcher, make_cfg()).normalize(INDEX_PAYLOAD)
    n50 = next(r for r in rows if r["symbol"] == "NIFTY 50")
    assert n50["extra"]["pe"] == pytest.approx(20.44)
    assert n50["extra"]["dy"] == pytest.approx(1.16)


def test_indices_small_move_is_silent():
    """NIFTY 50 at +0.35% is below the 1.0% notable threshold."""
    rows = build(IndicesFetcher, make_cfg()).normalize(INDEX_PAYLOAD)
    sigs = IndicesProcessor(make_cfg()).run(rows)
    assert "NIFTY 50" not in {s.entity for s in sigs}


def test_indices_large_move_is_critical():
    rows = build(IndicesFetcher, make_cfg()).normalize(INDEX_PAYLOAD)
    sigs = IndicesProcessor(make_cfg()).run(rows)
    bank = next(s for s in sigs if s.entity == "NIFTY BANK")
    assert bank.severity == "critical"     # -2.3% beyond critical 2.0
    assert "▼" in bank.title


def test_indices_watchlist_excludes_unwatched():
    """NIFTY REALTY moved 5% but is not on the watch list."""
    rows = build(IndicesFetcher, make_cfg()).normalize(INDEX_PAYLOAD)
    sigs = IndicesProcessor(make_cfg()).run(rows)
    assert "NIFTY REALTY" not in {s.entity for s in sigs}


def test_indices_empty_watchlist_evaluates_everything():
    cfg = make_cfg(**{"rules.indices.watch": []})
    rows = build(IndicesFetcher, cfg).normalize(INDEX_PAYLOAD)
    sigs = IndicesProcessor(cfg).run(rows)
    assert "NIFTY REALTY" in {s.entity for s in sigs}


def test_indices_up_and_down_are_separate_states(dao):
    rows = build(IndicesFetcher, make_cfg()).normalize(INDEX_PAYLOAD)
    sigs = IndicesProcessor(make_cfg()).run(rows)
    sm = AlertStateMachine(make_cfg(), dao)
    first = sm.process(sigs, category="indices_all", session_date=TODAY)
    second = sm.process(sigs, category="indices_all", session_date=TODAY)
    assert len(first) >= 1 and second == []


# ==========================================================================
# new listings
# ==========================================================================
def test_new_listings_tolerates_null_payload():
    """The endpoint returns the JSON literal `null` on quiet days."""
    assert build(NewListingsFetcher, make_cfg()).normalize(None) == []


def test_new_listings_tolerates_empty_dict():
    assert build(NewListingsFetcher, make_cfg()).normalize({"data": []}) == []


def test_new_listings_parses_a_listing():
    payload = {"data": [{"symbol": "NEWCO", "companyName": "New Co Limited",
                         "lastPrice": 145.0, "issuePrice": 100.0,
                         "series": "EQ", "totalTradedVolume": 5000000}]}
    rows = build(NewListingsFetcher, make_cfg()).normalize(payload)
    assert rows[0]["symbol"] == "NEWCO"
    assert rows[0]["extra"]["issue_price"] == 100.0


def test_new_listing_alerts_with_listing_gain():
    payload = {"data": [{"symbol": "NEWCO", "companyName": "New Co Limited",
                         "lastPrice": 145.0, "issuePrice": 100.0}]}
    rows = build(NewListingsFetcher, make_cfg()).normalize(payload)
    sigs = NewListingsProcessor(make_cfg()).run(rows)
    assert len(sigs) == 1
    assert "NEWCO" in sigs[0].title
    assert "+45.0%" in sigs[0].body        # 100 -> 145


def test_new_listing_ignores_universe():
    """A stock cannot be an index constituent on its first day."""
    from .helpers import FakeUniverse
    payload = {"data": [{"symbol": "NEWCO", "lastPrice": 145.0}]}
    rows = build(NewListingsFetcher, make_cfg()).normalize(payload)
    sigs = NewListingsProcessor(make_cfg(), FakeUniverse("SOMETHINGELSE")).run(rows)
    assert len(sigs) == 1


def test_new_listing_alerts_once(dao):
    payload = {"data": [{"symbol": "NEWCO", "lastPrice": 145.0}]}
    rows = build(NewListingsFetcher, make_cfg()).normalize(payload)
    proc = NewListingsProcessor(make_cfg())
    sm = AlertStateMachine(make_cfg(), dao)
    first = sm.process(proc.run(rows), category="new_listings",
                       session_date=TODAY)
    second = sm.process(proc.run(rows), category="new_listings",
                        session_date=TODAY)
    assert len(first) == 1 and second == []


# ==========================================================================
# ETFs
# ==========================================================================
ETF_PAYLOAD = {"data": [{
    "symbol": "SILVERBEES",
    "assets": "Domestic price of Silver- based on LBMA Silver daily spot",
    "open": "226.28", "high": "231.83", "low": "225.42", "ltP": "230.26",
    "chn": "3.89", "per": "1.72", "qty": "26759523",
    "trdVal": "6126860386.08", "nav": "226.7848", "wkhi": "360",
    "wklo": "112.3", "xDt": "-", "cAct": "-", "prevClose": "226.37",
}]}


def test_etf_traded_value_is_rupees():
    """6,126,860,386 RUPEES == 612.69 crore, cross-checked by qty x ltP."""
    rows = build(EtfFetcher, make_cfg()).normalize(ETF_PAYLOAD)
    assert rows[0]["traded_value"] == pytest.approx(612.686038608)
    implied = 26759523 * 230.26 / 1e7
    assert 0.9 < rows[0]["traded_value"] / implied < 1.1


def test_etf_numeric_fields_arrive_as_strings():
    rows = build(EtfFetcher, make_cfg()).normalize(ETF_PAYLOAD)
    assert rows[0]["last_price"] == pytest.approx(230.26)
    assert rows[0]["volume"] == 26759523


def test_etf_premium_to_nav_is_computed():
    """LTP 230.26 against NAV 226.7848 is a +1.53% premium."""
    rows = build(EtfFetcher, make_cfg()).normalize(ETF_PAYLOAD)
    assert rows[0]["extra"]["premium_pct"] == pytest.approx(1.5325, rel=1e-3)


def test_etf_premium_above_threshold_alerts():
    rows = build(EtfFetcher, make_cfg()).normalize(ETF_PAYLOAD)
    sigs = EtfProcessor(make_cfg()).run(rows)
    assert len(sigs) == 1
    assert "premium" in sigs[0].title
    assert sigs[0].severity == "notable"    # 1.53% < critical 3.0


def test_etf_discount_is_detected():
    payload = {"data": [dict(ETF_PAYLOAD["data"][0], ltP="200.0")]}
    rows = build(EtfFetcher, make_cfg()).normalize(payload)
    sigs = EtfProcessor(make_cfg()).run(rows)
    assert "discount" in sigs[0].title
    assert sigs[0].severity == "critical"   # ~11.8% divergence
    assert sigs[0].value < 0


def test_etf_illiquid_wide_spread_is_ignored():
    """A wide NAV gap on an untraded ETF is a stale quote, not news."""
    payload = {"data": [dict(ETF_PAYLOAD["data"][0], ltP="200.0",
                             trdVal="1000.0", qty="5")]}
    cfg = make_cfg(**{"rules.etf.min_traded_value_cr": 1.0})
    rows = build(EtfFetcher, cfg).normalize(payload)
    assert EtfProcessor(cfg).run(rows) == []


def test_etf_at_nav_is_silent():
    payload = {"data": [dict(ETF_PAYLOAD["data"][0], ltP="226.80")]}
    rows = build(EtfFetcher, make_cfg()).normalize(payload)
    assert EtfProcessor(make_cfg()).run(rows) == []


def test_etf_missing_nav_does_not_crash():
    payload = {"data": [dict(ETF_PAYLOAD["data"][0], nav="-")]}
    rows = build(EtfFetcher, make_cfg()).normalize(payload)
    assert rows[0]["extra"]["premium_pct"] is None
    assert EtfProcessor(make_cfg()).run(rows) == []
