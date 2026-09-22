"""設定・連携の状態診断（Web UI の「設定状況」と `transcribe doctor` で共用）。

各チェックは status を返す:
    ok    … 使える
    warn  … 動くが注意が必要（初回認証が必要など）
    error … 有効なのに設定が足りず動かない
    off   … 無効化されている（使わない設定）
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from .config import AppConfig

logger = logging.getLogger(__name__)


@dataclass
class Check:
    key: str
    label: str
    status: str
    detail: str
    hint: str = ""
    testable: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def collect_checks(cfg: AppConfig, glossary_path: Path | None = None) -> list[Check]:
    checks: list[Check] = []

    # ── 文字起こし（必須）
    ffmpeg = shutil.which("ffmpeg")
    checks.append(Check(
        "ffmpeg", "ffmpeg（音声変換）",
        "ok" if ffmpeg else "error",
        ffmpeg or "見つかりません",
        "" if ffmpeg else "ffmpeg をインストールして PATH に追加してください",
    ))
    t = cfg.transcription
    checks.append(Check(
        "whisper", "文字起こしモデル", "ok",
        f"{t.model}（{t.device} / {t.compute_type}）",
    ))
    browser = cfg.youtube.cookies_from_browser or ""
    use_cookies = browser.lower() not in ("", "none", "no", "false")
    checks.append(Check(
        "youtube", "YouTube Cookie",
        "ok" if use_cookies else "warn",
        f"{browser} から取得" if use_cookies else "Cookie なし",
        "限定公開動画の取得前に対象ブラウザを完全に終了してください" if use_cookies
        else "限定公開・年齢制限のある動画は取得できない場合があります",
    ))
    if glossary_path is not None:
        exists = glossary_path.exists()
        checks.append(Check(
            "glossary", "用語辞書",
            "ok" if exists else "warn",
            str(glossary_path.name) if exists else "glossary.yaml がありません",
            "" if exists else "誤認識の自動修正を使うには glossary.yaml を作成してください",
        ))

    # ── まとめ
    s = cfg.summarize
    if not s.enabled:
        checks.append(Check("summarize", "まとめ生成（LLM）", "off", "無効",
                            "config.yaml の summarize.enabled を true にすると自動でまとめを作成します"))
    elif not s.resolve_api_key():
        env = "GEMINI_API_KEY" if s.provider == "gemini" else "ANTHROPIC_API_KEY"
        checks.append(Check("summarize", "まとめ生成（LLM）", "error", f"{s.provider} の API キーが未設定",
                            f"config.yaml の summarize.*_api_key または環境変数 {env} を設定してください"))
    else:
        model = s.gemini_model if s.provider == "gemini" else s.anthropic_model
        checks.append(Check("summarize", "まとめ生成（LLM）", "ok", f"{s.provider} / {model}", testable=True))

    # ── Google Docs
    g = cfg.google_docs
    if not g.enabled:
        checks.append(Check("docs", "Google Docs 同期", "off", "無効"))
    elif not g.root_folder_id:
        checks.append(Check("docs", "Google Docs 同期", "error", "ルートフォルダ ID が未設定",
                            "config.yaml の google_docs.root_folder_id を設定してください"))
    elif not g.credentials_path.exists():
        checks.append(Check("docs", "Google Docs 同期", "error", f"{g.credentials_path.name} がありません",
                            "Google Cloud Console で OAuth クライアントを作成し配置してください"))
    elif not g.token_path.exists():
        checks.append(Check("docs", "Google Docs 同期", "warn", "未認証",
                            "CLI で `uv run transcribe sync` を一度実行し、ブラウザで認証してください"))
    else:
        checks.append(Check("docs", "Google Docs 同期", "ok", "認証済み"))

    # ── Notion
    n = cfg.notion
    if not n.enabled:
        checks.append(Check("notion", "Notion 同期", "off", "無効"))
    elif not n.token or not n.database_id:
        checks.append(Check("notion", "Notion 同期", "error", "token または database_id が未設定",
                            "config.yaml の notion.token と notion.database_id を設定してください"))
    else:
        detail = "動画 DB 設定済み" + ("・ローカル音声 DB 設定済み" if n.local_database_id else "")
        checks.append(Check("notion", "Notion 同期", "ok", detail, testable=True))

    # ── メール通知
    m = cfg.notification
    if not m.enabled:
        checks.append(Check("notification", "完了メール", "off", "無効"))
    elif not m.to_email:
        checks.append(Check("notification", "完了メール", "error", "送信先が未設定",
                            "config.yaml の notification.to_email を設定してください"))
    elif not g.credentials_path.exists():
        checks.append(Check("notification", "完了メール", "error", "Google の認証情報がありません",
                            "メール送信は Google Docs 同期と同じ credentials.json を使います"))
    else:
        checks.append(Check("notification", "完了メール", "ok", m.to_email))

    # ── ディスク
    work_mb = _dir_size_mb(cfg.work_dir)
    uploads_mb = _dir_size_mb(cfg.work_dir.parent / "uploads")
    checks.append(Check(
        "storage", "一時ファイル",
        "warn" if work_mb + uploads_mb > 5 * 1024 else "ok",
        f"作業フォルダ {work_mb:.0f} MB / アップロード {uploads_mb:.0f} MB",
        "ツール → 一時ファイル削除 で作業フォルダを空にできます" if work_mb > 0 else "",
    ))
    return checks


def run_connection_test(cfg: AppConfig, key: str) -> Check:
    """外部サービスへ実際に接続して設定を確認する（料金の発生しない API のみ使用）。"""
    try:
        if key == "notion":
            n = cfg.notion
            names = []
            for db_id in filter(None, [n.database_id, n.local_database_id]):
                resp = httpx.get(
                    f"https://api.notion.com/v1/databases/{db_id}",
                    headers={"Authorization": f"Bearer {n.token}", "Notion-Version": "2022-06-28"},
                    timeout=15,
                )
                resp.raise_for_status()
                title = "".join(t.get("plain_text", "") for t in resp.json().get("title", []))
                names.append(title or db_id)
            return Check("notion", "Notion 同期", "ok", "接続成功: " + " / ".join(names))
        if key == "summarize":
            s = cfg.summarize
            if s.provider == "gemini":
                from google import genai  # noqa: PLC0415

                genai.Client(api_key=s.resolve_api_key()).models.get(model=s.gemini_model)
                return Check("summarize", "まとめ生成（LLM）", "ok", f"接続成功: {s.gemini_model}")
            import anthropic  # noqa: PLC0415

            anthropic.Anthropic(api_key=s.resolve_api_key()).models.retrieve(s.anthropic_model)
            return Check("summarize", "まとめ生成（LLM）", "ok", f"接続成功: {s.anthropic_model}")
    except Exception as e:
        return Check(key, _TEST_LABELS.get(key, key), "error", f"接続失敗: {type(e).__name__}: {e}")
    return Check(key, _TEST_LABELS.get(key, key), "error", "この項目は接続テストに対応していません")


_TEST_LABELS = {"notion": "Notion 同期", "summarize": "まとめ生成（LLM）"}
