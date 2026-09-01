"""Phase 2, batch A: volume spurts, price bands, breadth, most active.

Payload fragments below are copied verbatim from live NSE responses captured
on 2026-08-29, misspellings and string/number inconsistencies included.

The unit assertions are the point of this module. Three equity feeds each
report "value traded" in a different unit, and a copy-paste between them would
be silent and catastrophic, so each conversion is pinned to a real number.
"""
from __future__ import annotations

from datetime import date

import pytest

from bbnse.fetchers.breadth import AdvanceDeclineFetcher
from bbnse.fetchers.most_active import MostActiveValueFetcher
from bbnse.fetchers.price_band import PriceBandHittersFetcher
from bbnse.fetchers.volume_spurts import VolumeSpurtsFetcher
from bbnse.processors.breadth import AdvanceDeclineProcessor
from bbnse.processors.most_active import MostActiveProcessor
from bbnse.processors.price_band import PriceBandProcessor
from bbnse.processors.state import AlertStateMachine
from bbnse.processors.volume_spurts import VolumeSpurtsProcessor

from .helpers import FakeUniverse, make_cfg

TODAY = date(2026, 8, 28)


class _NoNet:
    """Fetchers are exercised through normalize(); no session is needed."""
    def __init__(self, cfg):
        self.cfg = cfg


def build(fetcher_cls, cfg):
    f = fetcher_cls.__new__(fetcher_cls)
    f.cfg = cfg
    from bbnse.core.registry import load_registry
    f.registry = load_registry()
    return f


# ==========================================================================
# volume_spurts
# ==========================================================================
VOLUME_PAYLOAD = {"data": [{
    "symbol": "SPAL", "companyName": "S. P. Apparels Limited",
    "volume": 1289999, "week1AvgVolume": 186676,
    "week1volChange": 6.910332562244378, "week2AvgVolume": 134587,
    "week2volChange": 9.584818958908182, "ltp": 1021, "pChange": 7.14,
    "turnover": 8016.11985,
}]}


def test_volume_spurts_turnover_is_lakh():
    """turnover 8016.11985 LAKH == 80.16 crore. Verified against live data."""
    rows = build(VolumeSpurtsFetcher, make_cfg()).normalize(VOLUME_PAYLOAD)
    assert rows[0]["traded_value"] == pytest.approx(80.1611985)


def test_volume_spurts_change_field_is_a_multiple_not_a_percent():
    """week1volChange is 6.91x, not 6.91%.

    volume / week1AvgVolume = 1289999 / 186676 = 6.91036, against a reported
    6.91033 -- agreeing to five significant figures. The tiny gap is because
    NSE rounds week1AvgVolume in the payload but computed the ratio from the
    unrounded average, so the tolerance is loose on purpose.

    Reading this field as a percent would make a 7x spurt look like noise and
    nothing would ever alert.
    """
    rows = build(VolumeSpurtsFetcher, make_cfg()).normalize(VOLUME_PAYLOAD)
    m1 = rows[0]["extra"]["week1_multiple"]
    assert m1 == pytest.approx(1289999 / 186676, rel=1e-4)
    assert m1 > 6.0        # a multiple, not a 6.9% change


def test_volume_spurts_multiple_is_recomputed_when_absent():
    payload = {"data": [dict(VOLUME_PAYLOAD["data"][0], week1volChange=None)]}
    rows = build(VolumeSpurtsFetcher, make_cfg()).normalize(payload)
    assert rows[0]["extra"]["week1_multiple"] == pytest.approx(6.9103, rel=1e-3)


def test_volume_spurts_alerts_above_multiple():
    rows = build(VolumeSpurtsFetcher, make_cfg()).normalize(VOLUME_PAYLOAD)
    sigs = VolumeSpurtsProcessor(make_cfg(), FakeUniverse("SPAL")).run(rows)
    assert len(sigs) == 1
    assert sigs[0].severity == "critical"        # 9.58x >= critical 6.0
    assert "SPAL" in sigs[0].title


def test_volume_spurts_below_multiple_is_silent():
    cfg = make_cfg(**{"rules.volume_spurts.volume_vs_week1_avg_multiple": 20.0,
                      "rules.volume_spurts.volume_vs_week2_avg_multiple": 20.0})
    rows = build(VolumeSpurtsFetcher, cfg).normalize(VOLUME_PAYLOAD)
    assert VolumeSpurtsProcessor(cfg, FakeUniverse("SPAL")).run(rows) == []


def test_volume_spurts_respects_min_turnover():
    cfg = make_cfg(**{"rules.volume_spurts.min_traded_value_cr": 500.0})
    rows = build(VolumeSpurtsFetcher, cfg).normalize(VOLUME_PAYLOAD)
    assert VolumeSpurtsProcessor(cfg, FakeUniverse("SPAL")).run(rows) == []


def test_volume_spurts_respects_universe():
    rows = build(VolumeSpurtsFetcher, make_cfg()).normalize(VOLUME_PAYLOAD)
    assert VolumeSpurtsProcessor(make_cfg(), FakeUniverse("OTHER")).run(rows) == []


# ==========================================================================
# price_band_hitters
# ==========================================================================
BAND_PAYLOAD = {
    "upper": {"AllSec": {"data": [{
        "symbol": "MASTEK", "series": "EQ", "ltp": "1933.9",
        "change": "322.3", "pChange": "  20.00", "priceBand": "20",
        "highPrice": 1933.9, "lowPrice": 1615, "yearHigh": 2614,
        "yearLow": 1334.2, "totalTradedVol": 54.4906,
        "turnover": 1002.8722477,
    }], "count": {"TOTAL": "236", "UPPER": "146", "LOWER": "80",
                  "BOTH": "10"}}},
    "lower": {"AllSec": {"data": [{
        "symbol": "DEEDEV", "series": "EQ", "ltp": "610.95",
        "change": "-24.85", "pChange": "  -3.91", "priceBand": "5",
        "highPrice": 645, "lowPrice": 604.05, "yearHigh": 760,
        "yearLow": 183, "totalTradedVol": 4.54298,
        "turnover": 27.711269404000003,
    }]}},
}


def test_price_band_turnover_is_already_crore():
    """This feed reports CRORE while gainers reports LAKH. Same field name."""
    rows = build(PriceBandHittersFetcher, make_cfg()).normalize(BAND_PAYLOAD)
    upper = next(r for r in rows if r["symbol"] == "MASTEK")
    assert upper["traded_value"] == pytest.approx(1002.8722477)


def test_price_band_volume_is_lakh_shares():
    """54.4906 LAKH shares == 5,449,060 shares.

    Cross-check: 5,449,060 shares against 1002.87 crore implies an average
    price of 1,840, consistent with an LTP of 1,933.90 for a stock closing
    at its upper circuit.
    """
    rows = build(PriceBandHittersFetcher, make_cfg()).normalize(BAND_PAYLOAD)
    upper = next(r for r in rows if r["symbol"] == "MASTEK")
    assert upper["volume"] == 5449060
    implied = (upper["traded_value"] * 1e7) / upper["volume"]
    assert 0.9 < implied / upper["last_price"] < 1.05


def test_price_band_parses_space_padded_percent():
    """pChange arrives as the string '  20.00'."""
    rows = build(PriceBandHittersFetcher, make_cfg()).normalize(BAND_PAYLOAD)
    upper = next(r for r in rows if r["symbol"] == "MASTEK")
    assert upper["pct_change"] == pytest.approx(20.0)


def test_price_band_both_directions_are_captured():
    rows = build(PriceBandHittersFetcher, make_cfg()).normalize(BAND_PAYLOAD)
    assert {r["bucket"] for r in rows} == {"upper", "lower"}


def test_price_band_alerts_are_critical():
    rows = build(PriceBandHittersFetcher, make_cfg()).normalize(BAND_PAYLOAD)
    sigs = PriceBandProcessor(make_cfg(),
                              FakeUniverse("MASTEK", "DEEDEV")).run(rows)
    assert len(sigs) == 2
    assert all(s.severity == "critical" for s in sigs)


def test_price_band_upper_and_lower_are_separate_states(dao):
    """A stock swinging from upper to lower circuit must alert twice."""
    rows = build(PriceBandHittersFetcher, make_cfg()).normalize(BAND_PAYLOAD)
    sigs = PriceBandProcessor(make_cfg(),
                              FakeUniverse("MASTEK", "DEEDEV")).run(rows)
    assert len({s.state_bucket for s in sigs}) == 2


def test_price_band_alert_only_on_transition(dao):
    rows = build(PriceBandHittersFetcher, make_cfg()).normalize(BAND_PAYLOAD)
    proc = PriceBandProcessor(make_cfg(), FakeUniverse("MASTEK", "DEEDEV"))
    sm = AlertStateMachine(make_cfg(), dao)
    first = sm.process(proc.run(rows), category="price_band_hitters",
                       session_date=TODAY)
    second = sm.process(proc.run(rows), category="price_band_hitters",
                        session_date=TODAY)
    assert len(first) == 2 and second == []


def test_price_band_filters_penny_stocks():
    cfg = make_cfg(**{"rules.price_band.min_ltp": 5000.0})
    rows = build(PriceBandHittersFetcher, cfg).normalize(BAND_PAYLOAD)
    assert PriceBandProcessor(cfg, FakeUniverse("MASTEK", "DEEDEV")).run(rows) == []


def test_price_band_alert_on_filter():
    cfg = make_cfg(**{"rules.price_band.alert_on": ["upper"]})
    rows = build(PriceBandHittersFetcher, cfg).normalize(BAND_PAYLOAD)
    sigs = PriceBandProcessor(cfg, FakeUniverse("MASTEK", "DEEDEV")).run(rows)
    assert [s.payload["band"] for s in sigs] == ["upper"]


# ==========================================================================
# advance_decline
# ==========================================================================
BREADTH_PAYLOAD = {
    "timestamp": "28-Aug-2026 16:00:00",
    "advance": {
        "count": {"Advances": 1917, "Unchange": 126, "Declines": 1560,
                  "Total": 3603},
        "data": [], "indetifier": "Advances",
    },
}


def test_breadth_emits_one_summary_row_not_thousands():
    """The 525 KB payload must not become ~1900 observations per poll."""
    rows = build(AdvanceDeclineFetcher, make_cfg()).normalize(BREADTH_PAYLOAD)
    assert len(rows) == 1
    assert rows[0]["extra"]["advances"] == 1917
    assert rows[0]["extra"]["declines"] == 1560


def test_breadth_ratio_is_computed():
    rows = build(AdvanceDeclineFetcher, make_cfg()).normalize(BREADTH_PAYLOAD)
    assert rows[0]["extra"]["ad_ratio"] == pytest.approx(1917 / 1560)


def test_breadth_normal_market_does_not_alert():
    rows = build(AdvanceDeclineFetcher, make_cfg()).normalize(BREADTH_PAYLOAD)
    assert AdvanceDeclineProcessor(make_cfg()).run(rows) == []


def test_breadth_extreme_bullish_alerts():
    payload = {"advance": {"count": {"Advances": 3000, "Declines": 400,
                                     "Unchange": 100, "Total": 3500}}}
    rows = build(AdvanceDeclineFetcher, make_cfg()).normalize(payload)
    sigs = AdvanceDeclineProcessor(make_cfg()).run(rows)
    assert len(sigs) == 1 and "bullish" in sigs[0].title


def test_breadth_extreme_bearish_alerts():
    payload = {"advance": {"count": {"Advances": 300, "Declines": 3000,
                                     "Unchange": 100, "Total": 3400}}}
    rows = build(AdvanceDeclineFetcher, make_cfg()).normalize(payload)
    sigs = AdvanceDeclineProcessor(make_cfg()).run(rows)
    assert len(sigs) == 1 and "bearish" in sigs[0].title


def test_breadth_ignores_thin_early_session():
    payload = {"advance": {"count": {"Advances": 30, "Declines": 2,
                                     "Unchange": 0, "Total": 32}}}
    rows = build(AdvanceDeclineFetcher, make_cfg()).normalize(payload)
    assert AdvanceDeclineProcessor(make_cfg()).run(rows) == []


def test_breadth_survives_zero_declines():
    payload = {"advance": {"count": {"Advances": 500, "Declines": 0,
                                     "Unchange": 0, "Total": 500}}}
    rows = build(AdvanceDeclineFetcher, make_cfg()).normalize(payload)
    assert rows[0]["extra"]["ad_ratio"] == 500       # no ZeroDivisionError


# ==========================================================================
# most_active_value
# ==========================================================================
ACTIVE_PAYLOAD = {"data": [{
    "symbol": "TEMPSENS", "identifier": "TEMPSENSEQN", "lastPrice": 590.6,
    "pChange": -6.85, "quantityTraded": 51084119,
    "totalTradedVolume": 51084119, "totalTradedValue": 30362867810.03,
    "previousClose": 300, "exDate": "-", "purpose": None,
    "yearHigh": 634.85, "yearLow": 551.15, "change": -43.4, "open": 634,
    "closePrice": 586.65,
}]}


def test_most_active_value_is_rupees_not_lakh():
    """30,362,867,810 RUPEES == 3,036.29 crore.

    This is the highest-risk conversion in the project: the identical concept
    is LAKH in gainers and CRORE in price_band. Using lakh_to_cr here would
    report 303,628,678 crore and every row would look critical.
    """
    rows = build(MostActiveValueFetcher, make_cfg()).normalize(ACTIVE_PAYLOAD)
    assert rows[0]["traded_value"] == pytest.approx(3036.286781, rel=1e-6)
    # Sanity: qty * price must land in the same ballpark.
    implied = 51084119 * 590.6 / 1e7
    assert 0.9 < rows[0]["traded_value"] / implied < 1.1


def test_three_feeds_report_value_in_three_different_units():
    """Regression guard for the whole unit-audit exercise."""
    vol = build(VolumeSpurtsFetcher, make_cfg()).normalize(VOLUME_PAYLOAD)[0]
    band = build(PriceBandHittersFetcher, make_cfg()).normalize(BAND_PAYLOAD)[0]
    act = build(MostActiveValueFetcher, make_cfg()).normalize(ACTIVE_PAYLOAD)[0]
    # lakh/100, crore as-is, rupees/1e7 -- three different divisors.
    assert vol["traded_value"] == pytest.approx(8016.11985 / 100)
    assert band["traded_value"] == pytest.approx(1002.8722477)
    assert act["traded_value"] == pytest.approx(30362867810.03 / 1e7)


def test_most_active_alerts_above_threshold():
    rows = build(MostActiveValueFetcher, make_cfg()).normalize(ACTIVE_PAYLOAD)
    sigs = MostActiveProcessor(make_cfg(), FakeUniverse("TEMPSENS")).run(rows)
    assert len(sigs) == 1
    assert sigs[0].severity == "critical"       # 3036 cr >= 500 cr
    assert sigs[0].value == pytest.approx(3036.286781, rel=1e-6)


def test_most_active_below_threshold_is_silent():
    cfg = make_cfg(**{"rules.most_active.min_traded_value_cr": 99999.0})
    rows = build(MostActiveValueFetcher, cfg).normalize(ACTIVE_PAYLOAD)
    assert MostActiveProcessor(cfg, FakeUniverse("TEMPSENS")).run(rows) == []


def test_most_active_debounces(dao):
    rows = build(MostActiveValueFetcher, make_cfg()).normalize(ACTIVE_PAYLOAD)
    proc = MostActiveProcessor(make_cfg(), FakeUniverse("TEMPSENS"))
    sm = AlertStateMachine(make_cfg(), dao)
    first = sm.process(proc.run(rows), category="most_active_value",
                       session_date=TODAY)
    second = sm.process(proc.run(rows), category="most_active_value",
                        session_date=TODAY)
    assert len(first) == 1 and second == []


# ==========================================================================
# cross-feed interaction between the new equity categories
# ==========================================================================
def test_band_hit_escalates_over_an_existing_gainers_alert(dao):
    """price_band outranks gainers and is more severe, so it breaks through."""
    from bbnse.processors.correlate import CrossFeedDeduplicator
    from bbnse.processors.gainers_losers import GainersLosersProcessor

    cfg = make_cfg()
    dedup = CrossFeedDeduplicator(cfg, dao)
    gain_rows = [{"symbol": "MASTEK", "bucket": "allSec", "pct_change": 20.0,
                  "last_price": 1933.9, "traded_value": 1002.8, "extra": {}}]
    gain = GainersLosersProcessor(cfg, FakeUniverse("MASTEK"),
                                  category="gainers").run(gain_rows)
    assert len(dedup.apply(gain, session_date=TODAY)) == 1

    band_rows = build(PriceBandHittersFetcher, cfg).normalize(BAND_PAYLOAD)
    band = PriceBandProcessor(cfg, FakeUniverse("MASTEK")).run(band_rows)
    out = dedup.apply([s for s in band if s.entity == "MASTEK"],
                      session_date=TODAY)
    assert len(out) == 1
    assert out[0].severity == "critical"
    assert "gainers" in out[0].also_in
