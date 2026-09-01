"""Telegram notifier -- the primary real-time channel.

Setup is in the README; you need TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in
.env. A local token bucket caps sends per minute so a bad data day (or a
threshold set too low) cannot turn into hundreds of pushes.
"""
from __future__ import annotations

import html
import os
import time
from collections import deque
from pathlib import Path

import requests

from ..core.logging_setup import get_logger
from .base import AlertMessage, Notifier

log = get_logger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, cfg, settings: dict):
        super().__init__(cfg, settings)
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.parse_mode = settings.get("parse_mode", "HTML")
        self.rate_limit = int(settings.get("rate_limit_per_minute", 20))
        self.send_documents = bool(settings.get("send_reports_as_document", True))
        self._sent: deque[float] = deque()

    @property
    def enabled(self) -> bool:
        if not super().enabled:
            return False
        if not self.token or not self.chat_id:
            log.warning("telegram enabled but TELEGRAM_BOT_TOKEN / "
                        "TELEGRAM_CHAT_ID are not set in .env; skipping")
            return False
        return True

    # -- rate limiting -------------------------------------------------------
    def _throttle(self) -> bool:
        """False means the send should be dropped this minute."""
        now = time.time()
        while self._sent and now - self._sent[0] > 60:
            self._sent.popleft()
        if len(self._sent) >= self.rate_limit:
            return False
        self._sent.append(now)
        return True

    # -- formatting ----------------------------------------------------------
    def _format(self, m: AlertMessage) -> str:
        icon = m.SEV_ICON.get(m.severity, "•")
        tag = m.KIND_TAG.get(m.kind, "")
        ts = m.timestamp.strftime("%d-%b %H:%M:%S")
        if self.parse_mode == "HTML":
            parts = [f"{icon} <b>{html.escape(m.title)}</b>"
                     + (f"<i>{html.escape(tag)}</i>" if tag else "")]
            if m.body:
                parts.append(html.escape(m.body))
            parts.append(
                f"<code>{html.escape(m.category)}</code> · {m.severity} · {ts} IST"
            )
            return "\n".join(parts)
        return m.as_text()

    # -- transport -----------------------------------------------------------
    def _post(self, method: str, **data):
        return requests.post(API.format(token=self.token, method=method),
                             data=data, timeout=20)

    def send(self, message: AlertMessage) -> None:
        if not self._throttle():
            log.warning("telegram rate limit hit; alert dropped",
                        extra={"entity": message.entity,
                               "category": message.category})
            return
        try:
            resp = self._post(
                "sendMessage", chat_id=self.chat_id,
                text=self._format(message), parse_mode=self.parse_mode,
                disable_web_page_preview="true",
            )
            if resp.status_code != 200:
                log.error("telegram send failed",
                          extra={"status": resp.status_code,
                                 "resp": resp.text[:300]})
        except requests.RequestException as exc:
            log.error("telegram request error", extra={"err": str(exc)})

    def send_report(self, title: str, summary: str,
                    path: Path | None = None) -> None:
        try:
            if self.send_documents and path and Path(path).exists():
                with open(path, "rb") as fh:
                    resp = requests.post(
                        API.format(token=self.token, method="sendDocument"),
                        data={"chat_id": self.chat_id,
                              "caption": f"📄 {title}"[:1024]},
                        files={"document": (Path(path).name, fh)},
                        timeout=60,
                    )
                if resp.status_code != 200:
                    log.error("telegram document failed",
                              extra={"status": resp.status_code,
                                     "resp": resp.text[:300]})
                    return
            # Telegram caps messages at 4096 chars.
            body = summary if len(summary) < 3800 else summary[:3800] + "\n…"
            self._post("sendMessage", chat_id=self.chat_id,
                       text=f"📄 <b>{html.escape(title)}</b>\n\n"
                            f"{html.escape(body)}",
                       parse_mode="HTML", disable_web_page_preview="true")
        except requests.RequestException as exc:
            log.error("telegram report error", extra={"err": str(exc)})
