from __future__ import annotations

from functools import lru_cache

from ..config import AppConfig, load_config
from ..state import init_db
from .runner import PROJECT_ROOT

_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


@lru_cache(maxsize=1)
def _load_config() -> AppConfig:
    cfg = load_config(_CONFIG_PATH)
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    init_db(cfg.state_db)
    return cfg


def get_config() -> AppConfig:
    return _load_config()
