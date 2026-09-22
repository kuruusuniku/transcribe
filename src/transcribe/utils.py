import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def format_timestamp(seconds: float) -> str:
    """秒数を HH:MM:SS または MM:SS 形式に変換"""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def youtube_url_with_timestamp(video_id: str, seconds: float) -> str:
    t = int(seconds)
    return f"https://www.youtube.com/watch?v={video_id}&t={t}s"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_youtube_url(url: str) -> str:
    """YouTube の動画URLを https://www.youtube.com/watch?v=ID 形式に正規化する。

    youtu.be 短縮URL・shorts・live・embed や、t= / list= などの付加パラメータの違いで
    同じ動画が別ジョブとして登録されるのを防ぐ。YouTube 以外や判別できない URL はそのまま返す。
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]

    video_id: str | None = None
    if host == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/")[0]
    elif host in ("youtube.com", "music.youtube.com"):
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        else:
            parts = parsed.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
                video_id = parts[1]

    if video_id and _YOUTUBE_ID_RE.match(video_id):
        return f"https://www.youtube.com/watch?v={video_id}"
    return url
