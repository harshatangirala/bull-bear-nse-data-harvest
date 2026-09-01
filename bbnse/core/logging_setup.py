"""Structured logging.

Console stays human-readable; the file sink is JSON lines so a silently
failing fetcher can be found with a grep rather than by eyeballing.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

_CONFIGURED = False

# LogRecord attributes that are not caller-supplied context.
_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message", "asctime", "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Anything passed via extra={...} rides along as structured context.
        for key, val in record.__dict__.items():
            if key not in _STD and not key.startswith("_"):
                try:
                    json.dumps(val)
                    payload[key] = val
                except (TypeError, ValueError):
                    payload[key] = repr(val)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[38;5;244m", "INFO": "\033[38;5;39m",
        "WARNING": "\033[38;5;214m", "ERROR": "\033[38;5;196m",
        "CRITICAL": "\033[48;5;196m\033[97m",
    }
    RESET = "\033[0m"

    def __init__(self, color: bool = True):
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        lvl = record.levelname[:4]
        name = record.name.replace("bbnse.", "")
        base = f"{ts} {lvl:4s} {name:26s} {record.getMessage()}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        if self.color and record.levelname in self.COLORS:
            return f"{self.COLORS[record.levelname]}{base}{self.RESET}"
        return base


def setup_logging(level: str = "INFO", log_dir: str | Path = "logs",
                  color: bool = True) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(ConsoleFormatter(color=color))
    root.addHandler(console)

    # Keep 10 days of JSONL; that is long enough to diagnose a bad week.
    fileh = logging.handlers.TimedRotatingFileHandler(
        log_path / "bbnse.jsonl", when="midnight", backupCount=10,
        encoding="utf-8",
    )
    fileh.setLevel(logging.DEBUG)
    fileh.setFormatter(JsonFormatter())
    root.addHandler(fileh)

    # These are chatty and we already log our own request lifecycle.
    for noisy in ("urllib3", "apscheduler.executors", "apscheduler.scheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


# Keys logging reserves on LogRecord. Passing any of these via extra={} raises
# "Attempt to overwrite ... in LogRecord", which would otherwise take down a
# fetcher at runtime over a log line. Colliding keys are renamed, not dropped.
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


class SafeExtraLogger(logging.LoggerAdapter):
    """Logger that cannot be crashed by an unlucky `extra` key name."""

    def process(self, msg, kwargs):
        extra = kwargs.get("extra")
        if extra:
            kwargs["extra"] = {
                (f"ctx_{k}" if k in _RESERVED else k): v
                for k, v in extra.items()
            }
        return msg, kwargs


def get_logger(name: str) -> logging.LoggerAdapter:
    return SafeExtraLogger(logging.getLogger(name), {})
