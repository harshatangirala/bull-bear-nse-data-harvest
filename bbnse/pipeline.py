"""The pipeline: fetch -> store -> detect -> notify.

This is the one place that knows how a category is wired end to end. Adding a
category in phase 2 means adding a Fetcher, a Processor, and one line in
_CATEGORIES -- nothing else changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from .core.calendar import MarketCalendar
from .core.config import Config, load_config
from .core.health import HealthMonitor
from .core.logging_setup import get_logger
from .core.session import NSESession, get_session
from .core.universe import Universe
from .fetchers.base import BaseFetcher
from .fetchers.breadth import AdvanceDeclineFetcher
from .fetchers.closing_auction import ClosingAuctionFetcher
from .fetchers.derivatives import (
    DerivativesWatchFetcher, MostActiveContractsFetcher,
)
from .fetchers.etf import EtfFetcher
from .fetchers.flows import DailyReportsFetcher, FiiDiiFetcher
from .fetchers.gainers_losers import GainersFetcher, LosersFetcher
from .fetchers.indices import IndicesFetcher
from .fetchers.large_deals import LargeDealsFetcher
from .fetchers.most_active import MostActiveValueFetcher
from .fetchers.new_listings import NewListingsFetcher
from .fetchers.oi_spurts import OiSpurtsFetcher
from .fetchers.option_chain import OptionChainFetcher
from .fetchers.slb import SlbFetcher
from .fetchers.surveillance import (
    AsmFetcher, GsmFetcher, SurveillancePriceBandsFetcher,
)
from .fetchers.pre_open import PreOpenCashFetcher, PreOpenFnoFetcher
from .fetchers.price_band import PriceBandHittersFetcher
from .fetchers.volume_spurts import VolumeSpurtsFetcher
from .fetchers.week52 import Week52HighFetcher, Week52LowFetcher
from .notifiers.dispatch import Dispatcher
from .processors.base import BaseProcessor
from .processors.breadth import AdvanceDeclineProcessor
from .processors.correlate import CrossFeedDeduplicator
from .processors.derivatives import (
    DerivativesWatchProcessor, MostActiveContractsProcessor,
    OiSpurtsProcessor, OptionChainProcessor,
)
from .processors.closing_auction import ClosingAuctionProcessor
from .processors.etf import EtfProcessor
from .processors.flows import DailyReportsProcessor, FiiDiiProcessor
from .processors.gainers_losers import GainersLosersProcessor
from .processors.indices import IndicesProcessor
from .processors.large_deals import LargeDealsProcessor
from .processors.most_active import MostActiveProcessor
from .processors.new_listings import NewListingsProcessor
from .processors.slb import SlbProcessor
from .processors.surveillance import (
    AsmProcessor, GsmProcessor, SurveillancePriceBandsProcessor,
)
from .processors.pre_open import PreOpenProcessor
from .processors.price_band import PriceBandProcessor
from .processors.volume_spurts import VolumeSpurtsProcessor
from .processors.state import AlertStateMachine
from .processors.week52 import Week52Processor
from .storage.dao import Dao

log = get_logger(__name__)


@dataclass
class CategorySpec:
    name: str
    fetcher_cls: type[BaseFetcher]
    processor_factory: Callable[[Config, Universe], BaseProcessor]


# One entry per category. Adding a category means a Fetcher, a Processor and
# one line here -- see README > Adding a category.
_CATEGORIES: dict[str, CategorySpec] = {
    "gainers": CategorySpec(
        "gainers", GainersFetcher,
        lambda cfg, uni: GainersLosersProcessor(cfg, uni, category="gainers"),
    ),
    "losers": CategorySpec(
        "losers", LosersFetcher,
        lambda cfg, uni: GainersLosersProcessor(cfg, uni, category="losers"),
    ),
    "week52_high": CategorySpec(
        "week52_high", Week52HighFetcher,
        lambda cfg, uni: Week52Processor(cfg, uni, category="week52_high"),
    ),
    "week52_low": CategorySpec(
        "week52_low", Week52LowFetcher,
        lambda cfg, uni: Week52Processor(cfg, uni, category="week52_low"),
    ),
    "large_deals": CategorySpec(
        "large_deals", LargeDealsFetcher,
        lambda cfg, uni: LargeDealsProcessor(cfg, uni),
    ),
    # --- Phase 2, batch A: equity intraday ---
    "volume_spurts": CategorySpec(
        "volume_spurts", VolumeSpurtsFetcher,
        lambda cfg, uni: VolumeSpurtsProcessor(cfg, uni),
    ),
    "price_band_hitters": CategorySpec(
        "price_band_hitters", PriceBandHittersFetcher,
        lambda cfg, uni: PriceBandProcessor(cfg, uni),
    ),
    "advance_decline": CategorySpec(
        "advance_decline", AdvanceDeclineFetcher,
        lambda cfg, uni: AdvanceDeclineProcessor(cfg, uni),
    ),
    "most_active_value": CategorySpec(
        "most_active_value", MostActiveValueFetcher,
        lambda cfg, uni: MostActiveProcessor(cfg, uni),
    ),
    # --- Phase 2, batch B: indices, listings, ETFs ---
    "indices_all": CategorySpec(
        "indices_all", IndicesFetcher,
        lambda cfg, uni: IndicesProcessor(cfg, uni),
    ),
    "new_listings": CategorySpec(
        "new_listings", NewListingsFetcher,
        lambda cfg, uni: NewListingsProcessor(cfg, uni),
    ),
    "etf": CategorySpec(
        "etf", EtfFetcher,
        lambda cfg, uni: EtfProcessor(cfg, uni),
    ),
    # --- Phase 2, batch C: pre-open session ---
    "pre_open_cm": CategorySpec(
        "pre_open_cm", PreOpenCashFetcher,
        lambda cfg, uni: PreOpenProcessor(cfg, uni, category="pre_open_cm"),
    ),
    "pre_open_fo": CategorySpec(
        "pre_open_fo", PreOpenFnoFetcher,
        lambda cfg, uni: PreOpenProcessor(cfg, uni, category="pre_open_fo"),
    ),
    # --- Phase 2, batch D: derivatives ---
    "oi_spurts": CategorySpec(
        "oi_spurts", OiSpurtsFetcher,
        lambda cfg, uni: OiSpurtsProcessor(cfg, uni),
    ),
    "derivatives_watch": CategorySpec(
        "derivatives_watch", DerivativesWatchFetcher,
        lambda cfg, uni: DerivativesWatchProcessor(cfg, uni),
    ),
    "most_active_contracts": CategorySpec(
        "most_active_contracts", MostActiveContractsFetcher,
        lambda cfg, uni: MostActiveContractsProcessor(cfg, uni),
    ),
    "option_chain": CategorySpec(
        "option_chain", OptionChainFetcher,
        lambda cfg, uni: OptionChainProcessor(cfg, uni),
    ),
    # --- Phase 2, batch E: institutional flows and reports ---
    "fii_dii": CategorySpec(
        "fii_dii", FiiDiiFetcher,
        lambda cfg, uni: FiiDiiProcessor(cfg, uni),
    ),
    "daily_reports": CategorySpec(
        "daily_reports", DailyReportsFetcher,
        lambda cfg, uni: DailyReportsProcessor(cfg, uni),
    ),
    # --- Phase 3: the 3 originally-unresolved categories ---
    "gsm": CategorySpec(
        "gsm", GsmFetcher,
        lambda cfg, uni: GsmProcessor(cfg, uni),
    ),
    "asm": CategorySpec(
        "asm", AsmFetcher,
        lambda cfg, uni: AsmProcessor(cfg, uni),
    ),
    "surveillance_price_bands": CategorySpec(
        "surveillance_price_bands", SurveillancePriceBandsFetcher,
        lambda cfg, uni: SurveillancePriceBandsProcessor(cfg, uni),
    ),
    "slb": CategorySpec(
        "slb", SlbFetcher,
        lambda cfg, uni: SlbProcessor(cfg, uni),
    ),
    "closing_auction": CategorySpec(
        "closing_auction", ClosingAuctionFetcher,
        lambda cfg, uni: ClosingAuctionProcessor(cfg, uni),
    ),
}


def known_categories() -> list[str]:
    return list(_CATEGORIES)


@dataclass
class CycleResult:
    category: str
    rows: int = 0
    signals: int = 0
    merged: int = 0        # collapsed as cross-feed duplicates
    alerts: int = 0
    error: str = ""
    skipped: str = ""
    payload_changed: bool = True
    elapsed_sec: float = 0.0
    decisions: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error


class Pipeline:
    """Owns the shared resources; safe to reuse across many cycles."""

    def __init__(self, cfg: Config | None = None, *, dry_run: bool = False):
        self.cfg = cfg or load_config()
        self.dry_run = dry_run

        self.dao = Dao(self.cfg.db_url)
        self.dao.create_all()

        self.session: NSESession = get_session(self.cfg)
        self.calendar = MarketCalendar(self.cfg, self.session)
        self.universe = Universe(self.cfg, self.session)
        self.state = AlertStateMachine(self.cfg, self.dao)
        self.correlator = CrossFeedDeduplicator(self.cfg, self.dao)
        self.dispatcher = Dispatcher(self.cfg, self.dao)
        self.health = HealthMonitor(self.cfg, self.dao)

        self._fetchers: dict[str, BaseFetcher] = {}
        self._processors: dict[str, BaseProcessor] = {}

    # -- lazy component construction -----------------------------------------
    def fetcher(self, category: str) -> BaseFetcher:
        if category not in self._fetchers:
            spec = _CATEGORIES[category]
            self._fetchers[category] = spec.fetcher_cls(
                self.cfg, self.session, self.dao
            )
        return self._fetchers[category]

    def processor(self, category: str) -> BaseProcessor:
        if category not in self._processors:
            spec = _CATEGORIES[category]
            self._processors[category] = spec.processor_factory(
                self.cfg, self.universe
            )
        return self._processors[category]

    # -- one full cycle for one category -------------------------------------
    def run_category(self, category: str, *,
                     force: bool = False) -> CycleResult:
        if category not in _CATEGORIES:
            return CycleResult(category, error=f"unknown category '{category}'")

        fetcher = self.fetcher(category)
        outcome = fetcher.run()

        if not outcome.ok:
            self.health.record_failure(category, outcome.error)
            return CycleResult(category, error=outcome.error,
                               elapsed_sec=outcome.elapsed_sec)

        self.health.record_success(category, outcome.row_count,
                                   outcome.elapsed_sec)

        # An unchanged payload means nothing moved since the last poll. The
        # state machine would reach the same conclusions, so skip the work --
        # but never skip when the caller explicitly forced a run.
        if not outcome.payload_changed and not force:
            return CycleResult(category, rows=outcome.row_count,
                               skipped="payload unchanged since last poll",
                               payload_changed=False,
                               elapsed_sec=outcome.elapsed_sec)

        processor = self.processor(category)
        signals = processor.run(outcome.rows)

        trade_date = outcome.trade_date or self.calendar.now().date()

        # Collapse signals that describe one event reported by several feeds,
        # before the state machine turns them into alerts.
        raw_signal_count = len(signals)
        signals = self.correlator.apply(signals, session_date=trade_date)
        merged = raw_signal_count - len(signals)

        decisions = self.state.process(signals, category=category,
                                       session_date=trade_date)

        alerts = 0
        if decisions and not self.dry_run:
            alerts = self.dispatcher.dispatch(decisions, trade_date=trade_date)
        elif decisions:
            log.info("dry run: alerts not dispatched",
                     extra={"category": category, "count": len(decisions)})

        return CycleResult(category, rows=outcome.row_count,
                           signals=raw_signal_count, alerts=alerts,
                           merged=merged,
                           payload_changed=outcome.payload_changed,
                           elapsed_sec=outcome.elapsed_sec,
                           decisions=decisions)

    def run_categories(self, categories: list[str], *,
                       force: bool = False) -> list[CycleResult]:
        results = []
        for cat in categories:
            try:
                results.append(self.run_category(cat, force=force))
            except Exception as exc:
                log.exception("cycle crashed", extra={"category": cat})
                self.health.record_failure(cat, str(exc))
                results.append(CycleResult(cat, error=str(exc)))
        return results

    # -- session bookkeeping -------------------------------------------------
    def on_session_open(self, session_date: date | None = None) -> None:
        self.state.reset_session(session_date or self.calendar.now().date())

    def check_health(self) -> list[dict]:
        problems = self.health.problems()
        if problems and not self.dry_run:
            self.dispatcher.send_health_alert(problems)
        return problems

    def prune(self) -> dict[str, int]:
        return self.dao.prune(
            raw_days=int(self.cfg.get("storage.raw_retention_days", 30)),
            obs_days=int(self.cfg.get("storage.observation_retention_days", 400)),
        )
