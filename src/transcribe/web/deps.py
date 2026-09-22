from __future__ import annotations

from ..config import AppConfig, load_config
from ..state import init_db
from .runner import PROJECT_ROOT

_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# config.yaml の更新時刻が変わったら読み直す（Web サーバーの再起動なしで設定変更を反映）
_cache: tuple[float, AppConfig] | None = None


def _load_config() -> AppConfig:
    cfg = load_config(_CONFIG_PATH)
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    init_db(cfg.state_db)
    return cfg


def get_config() -> AppConfig:
    global _cache
    mtime = _CONFIG_PATH.stat().st_mtime if _CONFIG_PATH.exists() else 0.0
    if _cache is None or _cache[0] != mtime:
        _cache = (mtime, _load_config())
    return _cache[1]
