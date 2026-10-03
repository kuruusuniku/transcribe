"""API キー・トークンの取得と保存。

優先順位は「OS の保管庫（keyring）→ 環境変数 → config.yaml」。
keyring は Windows では資格情報マネージャー、macOS ではキーチェーンを使う。
keyring が入っていない・使えない環境では黙って次の手段に進むので、既存の使い方は壊れない。
環境変数 TRANSCRIBE_NO_KEYRING=1 で keyring の参照を止められる（テストやトラブル時用）。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

SERVICE_NAME = "transcribe"

# 登録名 → (環境変数名, 説明)
SECRETS: dict[str, tuple[str, str]] = {
    "gemini_api_key": ("GEMINI_API_KEY", "Gemini の API キー（summarize.gemini_api_key）"),
    "anthropic_api_key": ("ANTHROPIC_API_KEY", "Anthropic の API キー（summarize.anthropic_api_key）"),
    "notion_token": ("NOTION_TOKEN", "Notion のインテグレーショントークン（notion.token）"),
    "web_token": ("TRANSCRIBE_WEB_TOKEN", "Web UI のアクセストークン（web.token）"),
}


def _keyring():
    if os.environ.get("TRANSCRIBE_NO_KEYRING") == "1":
        return None
    try:
        import keyring
    except ImportError:
        return None
    return keyring


def _check_name(name: str) -> None:
    if name not in SECRETS:
        raise KeyError(f"未知の名前です: {name}（使える名前: {', '.join(SECRETS)}）")


def get_from_keyring(name: str) -> str:
    kr = _keyring()
    if kr is None:
        return ""
    try:
        return kr.get_password(SERVICE_NAME, name) or ""
    except Exception as e:  # 保管庫が無い Linux など
        logger.debug(f"keyring から {name} を読めませんでした: {e}")
        return ""


def get_secret(name: str, config_value: str = "") -> str:
    """keyring → 環境変数 → config.yaml の順で値を返す。どれも無ければ空文字。"""
    _check_name(name)
    env_var = SECRETS[name][0]
    return get_from_keyring(name) or os.environ.get(env_var, "") or config_value or ""


def secret_source(name: str, config_value: str = "") -> str:
    """値の出どころ（keyring / env / config / なし）を返す。doctor や list 表示用。"""
    _check_name(name)
    if get_from_keyring(name):
        return "keyring"
    if os.environ.get(SECRETS[name][0]):
        return "env"
    if config_value:
        return "config"
    return ""


def set_secret(name: str, value: str) -> None:
    _check_name(name)
    kr = _keyring()
    if kr is None:
        raise RuntimeError("keyring が使えません（`uv sync` で依存を入れてください）")
    kr.set_password(SERVICE_NAME, name, value)


def delete_secret(name: str) -> bool:
    _check_name(name)
    kr = _keyring()
    if kr is None:
        raise RuntimeError("keyring が使えません（`uv sync` で依存を入れてください）")
    try:
        kr.delete_password(SERVICE_NAME, name)
        return True
    except Exception:
        return False
