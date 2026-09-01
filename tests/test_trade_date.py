"""Regression coverage for the NULL trade_date bug.

Found live: 8 of the 20 fetchers never overrode `payload_trade_date()`, so
`BaseFetcher.persist()` wrote every observation row with `trade_date=NULL` --
938 rows across a single `once --all` run. Those rows silently drop out of
every report and retention query that filters on trade_date, with no
exception anywhere to surface it. The existing test suite never caught this
because every other test module calls `.normalize()` directly and never
touches `payload_trade_date()` or `.run()`.

Two layers of defense here: (1) `BaseFetcher.run()` now falls back to
"today in IST" so a future fetcher that forgets this override still gets a
usable date instead of NULL, and (2) the 7 fetchers whose payload actually
carries a timestamp parse it explicitly, since the payload's own date is more
correct than "today" when running late, after hours, or via catch-up replay.
This module pins both.
"""
from __future__ import annotations

from datetime import date

from bbnse.fetchers.base import _today_ist
from bbnse.fetchers.derivatives import (
    DerivativesWatchFetcher, MostActiveContractsFetcher,
)
from bbnse.fetchers.etf import EtfFetcher
from bbnse.fetchers.indices import IndicesFetcher
from bbnse.fetchers.most_active import MostActiveValueFetcher
from bbnse.fetchers.oi_spurts import OiSpurtsFetcher
from bbnse.fetchers.volume_spurts import VolumeSpurtsFetcher

from .helpers import make_cfg
from .test_phase2_equity import build


def test_today_ist_returns_a_date():
    assert isinstance(_today_ist(), date)


def test_volume_spurts_parses_its_own_timestamp():
    payload = {"timestamp": "28-Aug-2026 16:00:00", "data": []}
    fetcher = build(VolumeSpurtsFetcher, make_cfg())
    assert fetcher.payload_trade_date(payload) == date(2026, 8, 28)


def test_oi_spurts_prefers_curr_trading_date_over_timestamp():
    """currTradingDate is the more precise field when both are present."""
    payload = {"currTradingDate": "28-Aug-2026",
              "timestamp": "29-Aug-2026 09:00:00", "data": []}
    fetcher = build(OiSpurtsFetcher, make_cfg())
    assert fetcher.payload_trade_date(payload) == date(2026, 8, 28)


def test_oi_spurts_falls_back_to_timestamp_when_curr_trading_date_absent():
    payload = {"timestamp": "28-Aug-2026 15:40:07", "data": []}
    fetcher = build(OiSpurtsFetcher, make_cfg())
    assert fetcher.payload_trade_date(payload) == date(2026, 8, 28)


def test_most_active_value_parses_timestamp():
    payload = {"timestamp": "28-Aug-2026 16:00:00", "data": []}
    fetcher = build(MostActiveValueFetcher, make_cfg())
    assert fetcher.payload_trade_date(payload) == date(2026, 8, 28)


def test_indices_parses_timestamp():
    payload = {"timestamp": "28-Aug-2026 15:30", "data": []}
    fetcher = build(IndicesFetcher, make_cfg())
    assert fetcher.payload_trade_date(payload) == date(2026, 8, 28)


def test_etf_parses_timestamp_not_nav_date():
    """navDate lags the trading session by a day; timestamp is the poll date."""
    payload = {"timestamp": "28-Aug-2026 16:00:00", "navDate": "27-Aug-2026",
              "data": []}
    fetcher = build(EtfFetcher, make_cfg())
    assert fetcher.payload_trade_date(payload) == date(2026, 8, 28)


def test_derivatives_watch_parses_timestamp():
    payload = {"timestamp": "28-Aug-2026 15:40:00", "data": []}
    fetcher = build(DerivativesWatchFetcher, make_cfg())
    assert fetcher.payload_trade_date(payload) == date(2026, 8, 28)


def test_most_active_contracts_reads_timestamp_from_nested_bucket():
    """The timestamp sits inside data.volume/data.value, not at the top."""
    payload = {"volume": {"data": [], "timestamp": "28-Aug-2026 15:45:00"},
              "value": {"data": [], "timestamp": "28-Aug-2026 15:45:00"}}
    fetcher = build(MostActiveContractsFetcher, make_cfg())
    assert fetcher.payload_trade_date(payload) == date(2026, 8, 28)


def test_most_active_contracts_survives_missing_timestamp():
    payload = {"volume": {"data": []}, "value": {"data": []}}
    fetcher = build(MostActiveContractsFetcher, make_cfg())
    assert fetcher.payload_trade_date(payload) is None


def test_price_band_hitters_has_no_date_field_and_relies_on_fallback():
    """This endpoint carries no date anywhere; run() must supply one via
    _today_ist() rather than storing NULL."""
    from bbnse.fetchers.price_band import PriceBandHittersFetcher
    fetcher = build(PriceBandHittersFetcher, make_cfg())
    payload = {"upper": {"AllSec": {"data": []}}, "lower": {"AllSec": {"data": []}}}
    assert fetcher.payload_trade_date(payload) is None


def test_run_falls_back_to_today_when_payload_has_no_date(dao):
    """End-to-end: a fetcher whose payload_trade_date() returns None must
    still persist observations with a real date, not NULL."""
    from bbnse.fetchers.price_band import PriceBandHittersFetcher

    class _StubSession:
        def warm(self, referer):
            pass

        def get_json(self, url, referer=None, params=None, timeout=None):
            class R:
                ok = True
                status = 200
                json = {"upper": {"AllSec": {"data": [
                    {"symbol": "TESTCO", "series": "EQ", "ltp": "100",
                     "change": "20", "pChange": "20.0", "priceBand": "20",
                     "highPrice": 100, "lowPrice": 80, "yearHigh": 120,
                     "yearLow": 60, "totalTradedVol": 1.0, "turnover": 1.0}]}},
                          "lower": {"AllSec": {"data": []}}}
                content = b"{}"
            return R()

    captured = []

    class _StubDao:
        def store_snapshot(self, *a, **k):
            return (1, True)

        def add_observations(self, rows):
            captured.extend(rows)
            return len(rows)

    fetcher = PriceBandHittersFetcher(make_cfg(), _StubSession(), _StubDao())
    outcome = fetcher.run()

    assert outcome.ok
    assert len(captured) == 1
    assert captured[0]["trade_date"] is not None
    assert captured[0]["trade_date"] == _today_ist()
