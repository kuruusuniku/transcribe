from pathlib import Path


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
