"""Pipeline configuration loader.

Load order for each key:
  1. config.json  (committed — version-controlled, reproducible)
  2. Environment variable  (override for CI or local experiments)
  3. Hardcoded default

config.json holds non-secret pipeline parameters (model choices, thresholds).
.env holds only the OPENROUTER_API_KEY secret and is git-ignored.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"


def _load() -> dict[str, Any]:
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


_config: dict[str, Any] = _load()


def get(key: str, default: Any = None) -> Any:
    """Return a config value. config.json takes precedence over env vars."""
    if key in _config:
        return _config[key]
    env = os.environ.get(key)
    if env is not None:
        return env
    return default


def reload() -> None:
    """Re-read config.json from disk (useful in tests or after edits)."""
    global _config
    _config = _load()
