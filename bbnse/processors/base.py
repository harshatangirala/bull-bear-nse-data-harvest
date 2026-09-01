"""Processor contract.

A processor turns normalized rows into Signals. It says "this is interesting
and here is how interesting" -- it does not decide whether to notify. That
decision belongs to the state machine, which is what stops a stock sitting at
a 52-week high from alerting on every poll.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.logging_setup import get_logger

log = get_logger(__name__)

SEVERITY_ORDER = {"info": 0, "notable": 1, "critical": 2}


def severity_at_least(sev: str, floor: str) -> bool:
    return SEVERITY_ORDER.get(sev, 0) >= SEVERITY_ORDER.get(floor, 0)


@dataclass
class Signal:
    """One thing worth considering an alert about."""
    category: str
    rule_id: str
    entity: str                 # usually the symbol
    state_bucket: str           # discriminates states for the same entity
    severity: str               # info | notable | critical
    title: str
    body: str = ""
    value: float | None = None  # the number the alert is about
    payload: dict[str, Any] = field(default_factory=dict)

    # --- cross-feed correlation (see processors/correlate.py) --------------
    # Identity of the underlying real-world event, independent of which feed
    # reported it. Two signals sharing a dedup_key are the same event seen
    # twice, and collapse to one alert. None opts out entirely.
    dedup_key: str | None = None
    # Which correlation group this belongs to; only signals in the same group
    # are ever compared. Set from config, not by the processor.
    dedup_group: str | None = None
    # When several feeds report one event, the highest priority wins and the
    # others become corroborations. Lets the more specific feed be the
    # headline (a BLOCK deal outranks the same trade seen as a BULK deal).
    dedup_priority: int = 0
    # Human name for the feed/variant this signal came from. Defaults to the
    # category; set explicitly when one category carries several feeds, so the
    # tag reads "also in: bulk feed" rather than "also in: large_deals".
    dedup_label: str | None = None
    # Filled in by the deduplicator: other feeds that saw this event.
    also_in: list[str] = field(default_factory=list)

    @property
    def feed_label(self) -> str:
        return self.dedup_label or self.category


class BaseProcessor:
    """Subclasses set `category` / `rule_id` and implement evaluate()."""

    category: str = ""
    rule_id: str = ""
    config_key: str = ""        # section under `rules:` in config.yaml

    def __init__(self, cfg, universe=None):
        self.cfg = cfg
        self.universe = universe
        self.rules = cfg.section(f"rules.{self.config_key or self.category}")

    @property
    def enabled(self) -> bool:
        return bool(self.rules.get("enabled", True))

    def in_universe(self, symbol: str) -> bool:
        # No universe configured means "alert on everything".
        if self.universe is None:
            return True
        return symbol in self.universe

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        raise NotImplementedError

    def run(self, rows: list[dict]) -> list[Signal]:
        if not self.enabled:
            log.debug("processor disabled", extra={"category": self.category})
            return []
        try:
            signals = self.evaluate(rows)
        except Exception:
            log.exception("processor failed",
                          extra={"category": self.category})
            return []
        if signals:
            log.info("signals raised",
                     extra={"category": self.category, "count": len(signals),
                            "rows_in": len(rows)})
        return signals
