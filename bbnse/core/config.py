"""Config loading. Single source of truth is config.yaml + .env."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Config:
    """Dotted-path accessor over the parsed YAML.

    cfg.get("rules.week52.min_ltp", 0.0) beats cfg["rules"]["week52"][...]
    when a key may legitimately be absent.
    """

    def __init__(self, data: dict[str, Any], path: Path):
        self._data = data
        self.path = path

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        sentinel = object()
        val = self.get(dotted, sentinel)
        if val is sentinel:
            raise KeyError(f"Missing required config key '{dotted}' in {self.path}")
        return val

    def section(self, dotted: str) -> dict[str, Any]:
        return self.get(dotted, {}) or {}

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    # --- resolved paths -----------------------------------------------------
    def abs_path(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def db_url(self) -> str:
        # Env var wins so you can point at a scratch DB without editing YAML.
        env = os.getenv("BBNSE_DB_URL")
        if env:
            return env
        url = self.get("app.db_url", "sqlite:///data/bbnse.sqlite3")
        if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
            rel = url[len("sqlite:///"):]
            abs_p = self.abs_path(rel)
            abs_p.parent.mkdir(parents=True, exist_ok=True)
            return "sqlite:///" + abs_p.as_posix()
        return url


@lru_cache(maxsize=4)
def load_config(path: str | None = None) -> Config:
    load_dotenv(PROJECT_ROOT / ".env")
    cfg_path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"config.yaml not found at {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Config(data, cfg_path)
