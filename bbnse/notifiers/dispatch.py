"""Notification dispatch.

Alerts go out one at a time as they are detected -- never batched -- and each
delivery attempt is recorded so a silent channel failure is visible in the DB
rather than only in the logs.

To add a channel: implement Notifier, add it to _REGISTRY, add a stanza under
`notifiers:` in config.yaml.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..core.logging_setup import get_logger
from ..processors.state import AlertDecision
from ..storage.dao import Dao
from .base import AlertMessage, Notifier
from .console import ConsoleNotifier
from .telegram import TelegramNotifier

log = get_logger(__name__)

_REGISTRY: dict[str, type[Notifier]] = {
    "console": ConsoleNotifier,
    "telegram": TelegramNotifier,
    # "email": EmailNotifier,      # hook: implement + register
    # "discord": DiscordNotifier,  # hook: implement + register
}


class Dispatcher:
    def __init__(self, cfg, dao: Dao):
        self.cfg = cfg
        self.dao = dao
        self.tz = ZoneInfo(cfg.get("app.timezone", "Asia/Kolkata"))
        self.channels: list[Notifier] = []
        for name, settings in (cfg.section("notifiers") or {}).items():
            cls = _REGISTRY.get(name)
            if cls is None:
                # email/discord ship as disabled stubs; only complain when
                # someone actually switched one on.
                if (settings or {}).get("enabled"):
                    log.warning("notifier is enabled but not implemented",
                                extra={"notifier": name})
                else:
                    log.debug("notifier stub not implemented",
                              extra={"notifier": name})
                continue
            try:
                inst = cls(cfg, settings or {})
            except Exception as exc:
                log.error("notifier init failed",
                          extra={"notifier": name, "err": str(exc)})
                continue
            if inst.enabled:
                self.channels.append(inst)
        log.info("dispatcher ready",
                 extra={"channels": [c.name for c in self.channels]})

    def _message(self, decision: AlertDecision) -> AlertMessage:
        sig = decision.signal
        return AlertMessage(
            category=sig.category, entity=sig.entity, severity=sig.severity,
            kind=decision.kind, title=sig.title, body=sig.body,
            timestamp=datetime.now(self.tz), value=sig.value,
            payload=sig.payload,
        )

    def dispatch(self, decisions: list[AlertDecision],
                 trade_date: date | None = None) -> int:
        """Persist and deliver each alert individually. Returns count sent."""
        sent = 0
        for decision in decisions:
            sig = decision.signal
            msg = self._message(decision)

            alert = self.dao.record_alert(
                event_key=decision.event_key, category=sig.category,
                rule_id=sig.rule_id, entity=sig.entity, severity=sig.severity,
                kind=decision.kind, title=sig.title, body=sig.body,
                value=sig.value, trade_date=trade_date, payload=sig.payload,
            )

            for channel in self.channels:
                if not channel.accepts(msg):
                    continue
                try:
                    channel.send(msg)
                    self.dao.record_delivery(alert.id, channel.name,
                                             delivered=True)
                except Exception as exc:
                    log.error("delivery failed",
                              extra={"channel": channel.name,
                                     "entity": sig.entity, "err": str(exc)})
                    self.dao.record_delivery(alert.id, channel.name,
                                             delivered=False, error=str(exc))
            sent += 1
        return sent

    def send_report(self, title: str, summary: str,
                    path: Path | None = None) -> None:
        for channel in self.channels:
            try:
                channel.send_report(title, summary, path)
            except Exception as exc:
                log.error("report delivery failed",
                          extra={"channel": channel.name, "err": str(exc)})

    def send_health_alert(self, problems: list[dict]) -> None:
        if not problems:
            return
        severity = self.cfg.get("health.severity", "critical")
        for p in problems:
            msg = AlertMessage(
                category="health", entity=p["category"], severity=severity,
                kind="new",
                title=f"🩺 Fetcher '{p['category']}' is {p['kind']}",
                body=p["detail"], timestamp=datetime.now(self.tz),
            )
            for channel in self.channels:
                if channel.accepts(msg):
                    try:
                        channel.send(msg)
                    except Exception as exc:
                        log.error("health alert failed",
                                  extra={"channel": channel.name,
                                         "err": str(exc)})
