from __future__ import annotations

import base64
import logging
from email.mime.text import MIMEText

from googleapiclient.discovery import build

from .config import AppConfig
from .sync import get_credentials

logger = logging.getLogger(__name__)


def _build_report_body(jobs: list[dict]) -> str:
    lines: list[str] = []

    done_count = sum(1 for j in jobs if j["status"] == "done")
    failed_count = sum(1 for j in jobs if j["status"] == "failed")
    total = len(jobs)

    lines.append(f"処理結果: {total}件 (成功 {done_count} / 失敗 {failed_count})")
    lines.append("")
    lines.append("=" * 50)

    for i, job in enumerate(jobs, 1):
        title = job["title"] or job["url"]
        status = job["status"]

        lines.append(f"\n{i}. {title}")

        if status == "done":
            transcribe_mark = "✓"
            summary_mark = "✓" if job.get("summarized_at") else "—"
            docs_mark = "✓" if job.get("synced_at") else "—"
            notion_mark = "✓" if job.get("notion_synced_at") else "—"
            lines.append(f"   文字起こし: {transcribe_mark} | まとめ: {summary_mark} | Docs: {docs_mark} | Notion: {notion_mark}")
            post_error = job.get("post_error")
            if post_error:
                for err_line in post_error.splitlines():
                    lines.append(f"   ⚠ 後処理失敗: {err_line}")
        else:
            error = job.get("error_message", "")
            first_line = error.split("\n")[0] if error else "不明なエラー"
            lines.append(f"   ✗ 失敗: {first_line}")

    warn_count = sum(1 for j in jobs if j["status"] == "done" and j.get("post_error"))
    if warn_count:
        lines.append("")
        lines.append("=" * 50)
        lines.append(
            f"後処理が失敗したジョブが {warn_count} 件あります。"
            "`transcribe summarize` / `transcribe sync-notion` / `transcribe sync` で再実行できます。"
        )

    return "\n".join(lines)


def send_batch_report(jobs: list[dict], cfg: AppConfig) -> None:
    """バッチ完了後のサマリーメールを送信する。"""
    if not cfg.notification.enabled or not cfg.notification.to_email:
        return

    if not cfg.google_docs.credentials_path.exists():
        logger.warning("credentials.json が見つかりません。メール通知をスキップします。")
        return

    done_count = sum(1 for j in jobs if j["status"] == "done")
    failed_count = sum(1 for j in jobs if j["status"] == "failed")
    total = len(jobs)

    subject = f"[Transcribe] バッチ完了 - {total}件処理 (成功{done_count} / 失敗{failed_count})"
    body = _build_report_body(jobs)

    try:
        creds = get_credentials(cfg.google_docs.credentials_path, cfg.google_docs.token_path)
        service = build("gmail", "v1", credentials=creds)

        message = MIMEText(body, "plain", "utf-8")
        message["to"] = cfg.notification.to_email
        message["subject"] = subject

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        service.users().messages().send(userId="me", body={"raw": raw}).execute()

        logger.info(f"バッチ結果メールを送信しました: {cfg.notification.to_email}")
    except Exception as e:
        logger.warning(f"メール送信失敗（バッチ処理自体は正常完了）: {e}")
