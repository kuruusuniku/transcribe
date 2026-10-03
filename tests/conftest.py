"""テスト共通の設定。"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def no_notion_schema_request():
    """Notion の DB プロパティ構成取得が実ネットワークに出ないようにする。

    構成に合わせる挙動を検証するテストは、内側で _get_db_schema を patch して上書きする。
    HTTP 取得そのものは _fetch_db_schema のテストで検証する。
    """
    from transcribe import notion_sync

    notion_sync._schema_cache.clear()
    with patch.object(notion_sync, "_get_db_schema", return_value=None):
        yield


@pytest.fixture(autouse=True)
def isolate_secrets(monkeypatch):
    """実機の OS 保管庫（keyring）や環境変数のキーがテストに混ざらないようにする。"""
    monkeypatch.setenv("TRANSCRIBE_NO_KEYRING", "1")
    for var in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "NOTION_TOKEN", "TRANSCRIBE_WEB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
