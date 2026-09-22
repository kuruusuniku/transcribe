from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from transcribe.stages.downloader import download_audio


def _cfg():
    return SimpleNamespace(youtube=SimpleNamespace(cookies_from_browser="none"))


def _mock_ydl(info):
    ydl = MagicMock()
    ydl.__enter__.return_value = ydl
    ydl.extract_info.return_value = info
    ydl.process_ie_result.return_value = info
    return ydl


def test_skips_download_when_audio_exists(tmp_path):
    (tmp_path / "vid12345678.mp3").write_bytes(b"x")
    info = {"id": "vid12345678", "title": "t", "upload_date": "20250101"}
    ydl = _mock_ydl(info)
    with patch("yt_dlp.YoutubeDL", return_value=ydl), patch(
        "transcribe.stages.downloader._ensure_deno_in_path"
    ):
        result = download_audio("https://www.youtube.com/watch?v=vid12345678", tmp_path, _cfg())

    ydl.process_ie_result.assert_not_called()
    assert result.audio_path == tmp_path / "vid12345678.mp3"


def test_downloads_when_audio_missing(tmp_path):
    info = {"id": "vid12345678", "title": "t", "upload_date": "20250101"}
    ydl = _mock_ydl(info)
    ydl.process_ie_result.side_effect = lambda *a, **k: (
        (tmp_path / "vid12345678.mp3").write_bytes(b"x") and info
    )
    with patch("yt_dlp.YoutubeDL", return_value=ydl), patch(
        "transcribe.stages.downloader._ensure_deno_in_path"
    ):
        result = download_audio("https://www.youtube.com/watch?v=vid12345678", tmp_path, _cfg())

    ydl.process_ie_result.assert_called_once()
    assert result.title == "t"
