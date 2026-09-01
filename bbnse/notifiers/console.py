"""Console notifier -- the default, and what `test-alert` demonstrates.

Always available, needs no credentials, and doubles as the dry-run channel
while you tune thresholds in config.yaml.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .base import AlertMessage, Notifier

_COLOR = {
    "info": "\033[38;5;39m",
    "notable": "\033[38;5;214m",
    "critical": "\033[1m\033[38;5;196m",
}
_RESET = "\033[0m"
_DIM = "\033[38;5;244m"


# Used when the terminal encoding cannot represent the real glyphs. Windows
# consoles default to cp1252; main.py reconfigures stdout to UTF-8, but output
# piped to a file or captured by a service manager can still land on cp1252.
_ASCII_FALLBACK = {
    "▲": "^", "▼": "v", "•": "*", "·": "-",
    "ℹ️": "[i]", "⚠️": "[!]", "\U0001f6a8": "[!!]",
    "\U0001f4c4": "[doc]", "\U0001fa7a": "[health]",
}


def _stream_supports_unicode() -> bool:
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "▲\U0001f6a8".encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self, cfg, settings: dict):
        super().__init__(cfg, settings)
        self.color = sys.stdout.isatty()
        self.unicode_ok = _stream_supports_unicode()

    def _paint(self, text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if self.color else text

    def _safe(self, text: str) -> str:
        """An alert must never be lost to a terminal encoding limitation."""
        if self.unicode_ok:
            return text
        for glyph, plain in _ASCII_FALLBACK.items():
            text = text.replace(glyph, plain)
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        return text.encode(enc, errors="replace").decode(enc)

    def send(self, message: AlertMessage) -> None:
        icon = message.SEV_ICON.get(message.severity, "•")
        tag = message.KIND_TAG.get(message.kind, "")
        head = self._paint(f"{icon} {message.title}{tag}",
                           _COLOR.get(message.severity, ""))
        ts = message.timestamp.strftime("%d-%b %H:%M:%S")
        meta = self._paint(
            f"   {message.category} · {message.severity} · {ts} IST", _DIM
        )
        print(self._safe(head))
        if message.body:
            print(self._safe(f"   {message.body}"))
        print(self._safe(meta))

    def send_report(self, title: str, summary: str,
                    path: Path | None = None) -> None:
        print(self._safe(self._paint(f"\n📄 {title}", _COLOR["info"])))
        if summary:
            print(self._safe(summary))
        if path:
            print(self._safe(self._paint(f"   written to {path}", _DIM)))
