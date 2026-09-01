"""Notifier interface.

Adding a channel means subclassing Notifier and registering it in
dispatch.py. Nothing else in the codebase knows which channels exist.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..processors.base import SEVERITY_ORDER


@dataclass
class AlertMessage:
    """What a channel is asked to deliver. Short and scannable by design."""
    category: str
    entity: str
    severity: str
    kind: str                     # new | escalation | reminder
    title: str
    body: str
    timestamp: datetime
    value: float | None = None
    payload: dict[str, Any] | None = None

    SEV_ICON = {"info": "ℹ️", "notable": "⚠️", "critical": "🚨"}
    KIND_TAG = {"new": "", "escalation": " [ESCALATED]", "reminder": " [STILL OPEN]"}

    def as_text(self) -> str:
        icon = self.SEV_ICON.get(self.severity, "•")
        tag = self.KIND_TAG.get(self.kind, "")
        ts = self.timestamp.strftime("%d-%b %H:%M:%S")
        lines = [f"{icon} {self.title}{tag}"]
        if self.body:
            lines.append(self.body)
        lines.append(f"{self.category} · {self.severity} · {ts} IST")
        return "\n".join(lines)


class Notifier:
    name: str = "base"

    def __init__(self, cfg, settings: dict):
        self.cfg = cfg
        self.settings = settings or {}
        self.min_severity = self.settings.get("min_severity", "info")

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", False))

    def accepts(self, message: AlertMessage) -> bool:
        return (SEVERITY_ORDER.get(message.severity, 0)
                >= SEVERITY_ORDER.get(self.min_severity, 0))

    def send(self, message: AlertMessage) -> None:
        raise NotImplementedError

    def send_report(self, title: str, summary: str,
                    path: Path | None = None) -> None:
        """Optional: deliver a generated report. Default is a plain message."""
        self.send(AlertMessage(
            category="report", entity="", severity="info", kind="new",
            title=title, body=summary, timestamp=datetime.now(),
        ))
