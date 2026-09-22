from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..config import AppConfig

logger = logging.getLogger(__name__)


def _ensure_deno_in_path() -> None:
    """denoがPATHにない場合、典型的なインストール先を探して追加する。"""
    if shutil.which("deno"):
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates: list[Path] = [
        Path.home() / ".deno" / "bin",
        Path(local_app_data) / "deno" / "bin",
    ]
    # winget インストール先を探索
    winget_pkgs = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
    if winget_pkgs.exists():
        candidates.extend(winget_pkgs.glob("DenoLand.Deno_*"))

    for candidate in candidates:
        exe = candidate / ("deno.exe" if os.name == "nt" else "deno")
        if exe.exists():
            os.environ["PATH"] = str(candidate) + os.pathsep + os.environ.get("PATH", "")
            logger.info(f"deno を PATH に追加: {candidate}")
            return

    logger.warning("deno が見つかりません。YouTube n challenge の解決に失敗する可能性があります。")


@dataclass
class DownloadResult:
    video_id: str
    title: str
    url: str
    audio_path: Path
    upload_date: str | None  # YYYYMMDD or None


def download_audio(url: str, work_dir: Path, cfg: AppConfig) -> DownloadResult:
    """YouTube URLから音声をダウンロードし、DownloadResult を返す"""
    import yt_dlp  # noqa: PLC0415

    _ensure_deno_in_path()

    out_template = str(work_dir / "%(id)s.%(ext)s")

    browser = cfg.youtube.cookies_from_browser
    use_cookies = browser and browser.lower() not in ("none", "no", "false", "")

    ydl_opts: dict = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": False,
        # deno の npm パッケージキャッシュを許可（初回のみダウンロード、以降はキャッシュ使用）
        "remote_components": ["ejs:npm"],
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }
    if use_cookies:
        ydl_opts["cookiesfrombrowser"] = (browser,)

    logger.info(f"ダウンロード開始: {url}  (cookie: {browser if use_cookies else 'なし'})")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # メタデータを先に取得し、リトライ等で既にダウンロード済みの音声があれば再ダウンロードしない
            info = ydl.extract_info(url, download=False)
            existing = work_dir / f"{info['id']}.mp3"
            if existing.exists() and existing.stat().st_size > 0:
                logger.info(f"ダウンロード済みの音声を再利用: {existing.name}")
            else:
                info = ydl.process_ie_result(info, download=True)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "cookies" in msg.lower() or "browser" in msg.lower():
            logger.error(
                f"Cookie取得エラー: {browser} ブラウザが完全に終了しているか確認してください。"
            )
        raise

    video_id: str = info["id"]
    title: str = info.get("title", video_id)
    upload_date: str | None = info.get("upload_date")

    audio_path = work_dir / f"{video_id}.mp3"
    if not audio_path.exists():
        # yt-dlp が別の拡張子で保存した場合を探す
        candidates = list(work_dir.glob(f"{video_id}.*"))
        if not candidates:
            raise FileNotFoundError(f"ダウンロードした音声ファイルが見つかりません: {video_id}")
        audio_path = candidates[0]

    logger.info(f"ダウンロード完了: {title!r}  → {audio_path.name}")
    return DownloadResult(
        video_id=video_id,
        title=title,
        url=url,
        audio_path=audio_path,
        upload_date=upload_date,
    )
