from __future__ import annotations

import logging
import re
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import GoogleDocsConfig

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def get_credentials(credentials_path: Path, token_path: Path) -> Credentials:
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def get_or_create_subfolder(service, parent_id: str, name: str) -> str:
    query = (
        f"'{parent_id}' in parents"
        f" and name = '{name}'"
        f" and mimeType = 'application/vnd.google-apps.folder'"
        f" and trashed = false"
    )
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    logger.info(f"サブフォルダ作成: {name}")
    return folder["id"]


def _extract_title(markdown_path: Path) -> str:
    try:
        for line in markdown_path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^#\s+(.+)", line)
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return markdown_path.stem


def upload_as_google_doc(
    service,
    folder_id: str,
    title: str,
    markdown_path: Path,
) -> str:
    metadata = {
        "name": title,
        "mimeType": "application/vnd.google-apps.document",
        "parents": [folder_id],
    }
    media = MediaFileUpload(str(markdown_path), mimetype="text/markdown", resumable=True)
    doc = service.files().create(body=metadata, media_body=media, fields="id").execute()
    doc_id = doc["id"]
    logger.info(f"Google Docs アップロード完了: {title} (id={doc_id})")
    return doc_id


def sync_job(
    job: dict,
    output_dir: Path,
    cfg: GoogleDocsConfig,
) -> str | None:
    """transcript.md と（存在すれば）summary.md を Google Docs に同期する。

    成功時は transcript.md の Google Docs ID を返す。
    summary.md がなくても transcript.md の同期は正常に行う。
    """
    transcript_path = output_dir / "transcript.md"
    if not transcript_path.exists():
        logger.warning(f"transcript.md が見つかりません: {output_dir}")
        return None

    if not cfg.credentials_path.exists():
        raise FileNotFoundError(
            f"credentials.json が見つかりません: {cfg.credentials_path}\n"
            "Google Cloud Console で OAuth クライアント ID を作成し、"
            "credentials.json をプロジェクトルートに配置してください。"
        )

    creds = get_credentials(cfg.credentials_path, cfg.token_path)
    service = build("drive", "v3", credentials=creds)

    source_type = job.get("source_type", "youtube")
    subfolder_name = "local" if source_type == "local" else "youtube"
    folder_id = get_or_create_subfolder(service, cfg.root_folder_id, subfolder_name)

    title = _extract_title(transcript_path)
    doc_id = upload_as_google_doc(service, folder_id, title, transcript_path)

    summary_path = output_dir / "summary.md"
    if summary_path.exists():
        try:
            upload_as_google_doc(service, folder_id, f"{title}（まとめ）", summary_path)
        except Exception as e:
            logger.warning(f"summary.md の同期失敗（transcript は同期済み）: {e}")

    return doc_id
