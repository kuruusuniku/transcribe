from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

from ..config import AppConfig
from ..postprocess import ProcessedSegment
from ..utils import format_timestamp_hms, youtube_url_with_timestamp

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


def format_outputs(
    segments: Iterable[ProcessedSegment],
    video_id: str,
    url: str,
    title: str,
    upload_date: str | None,
    audio_separation_enabled: bool,
    cfg: AppConfig,
    output_dir: Path,
    source_type: str = "youtube",
) -> int:
    """Markdown と JSON を output_dir に書き出す。処理セグメント数を返す。"""
    output_dir.mkdir(parents=True, exist_ok=True)

    # ストリームをここで初めてリスト化（低信頼数カウント・複数ファイル出力に必要）
    seg_list = list(segments)

    now = datetime.now(tz=JST)
    transcribed_at = now.isoformat(timespec="seconds")

    recording_date = _parse_upload_date(upload_date)
    low_conf_count = sum(1 for s in seg_list if s.low_confidence)

    _write_markdown(
        segments=seg_list,
        video_id=video_id,
        url=url,
        title=title,
        recording_date=recording_date,
        transcribed_at=transcribed_at,
        audio_separation_enabled=audio_separation_enabled,
        cfg=cfg,
        low_conf_count=low_conf_count,
        output_dir=output_dir,
        source_type=source_type,
    )

    _write_segments_json(
        segments=seg_list,
        video_id=video_id,
        url=url,
        title=title,
        transcribed_at=transcribed_at,
        audio_separation_enabled=audio_separation_enabled,
        cfg=cfg,
        output_dir=output_dir,
        source_type=source_type,
    )

    _write_meta_json(
        video_id=video_id,
        url=url,
        title=title,
        recording_date=recording_date,
        transcribed_at=transcribed_at,
        audio_separation_enabled=audio_separation_enabled,
        cfg=cfg,
        low_conf_count=low_conf_count,
        segment_count=len(seg_list),
        output_dir=output_dir,
        source_type=source_type,
        duration_seconds=seg_list[-1].end if seg_list else 0.0,
    )

    logger.info(f"出力完了: {output_dir}")
    return len(seg_list)


def _parse_upload_date(upload_date: str | None) -> str:
    if not upload_date:
        return "不明"
    try:
        d = datetime.strptime(upload_date, "%Y%m%d")
        return d.strftime("%Y-%m-%d")
    except ValueError:
        return upload_date


def rebuild_outputs(output_dir: Path, cfg: AppConfig) -> int:
    """segments.json をもとに transcript.md / meta.json を作り直す（再文字起こしはしない）。

    出力の書式を変えたときに、既存ジョブへ反映するために使う。
    """
    data = json.loads((output_dir / "segments.json").read_text(encoding="utf-8"))
    meta = {}
    meta_path = output_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

    segments = [
        ProcessedSegment(
            start=s["start"],
            end=s["end"],
            text=s["text"],
            avg_logprob=s.get("avg_logprob", 0.0),
            no_speech_prob=s.get("no_speech_prob", 0.0),
            low_confidence=s.get("low_confidence", False),
            important_term_hit=s.get("important_term_hit", False),
            original_text=s.get("original_text"),
        )
        for s in data.get("segments", [])
    ]

    recording_date = meta.get("recording_date") or "不明"
    upload_date = recording_date.replace("-", "") if recording_date != "不明" else None
    snapshot = meta.get("config_snapshot", {})

    return format_outputs(
        segments=segments,
        video_id=data.get("video_id", ""),
        url=data.get("url", ""),
        title=data.get("title", ""),
        upload_date=upload_date,
        audio_separation_enabled=bool(snapshot.get("audio_separation", data.get("config", {}).get("audio_separation"))),
        cfg=cfg,
        output_dir=output_dir,
        source_type=data.get("source_type", "youtube"),
    )


def group_segments(
    segments: list[ProcessedSegment],
    *,
    max_span_seconds: int,
    gap_seconds: float,
) -> list[list[ProcessedSegment]]:
    """セグメントを見出し単位のまとまりに分ける。

    見出しの時刻は「実際に話し始めた時刻」にするため、固定間隔で区切らず、
    無音（gap_seconds 以上の間）で区切る。区切りが長くなりすぎる場合は
    max_span_seconds を超えたところで、次の発話の開始時刻から新しい区切りにする。
    """
    groups: list[list[ProcessedSegment]] = []
    for seg in segments:
        if groups:
            prev = groups[-1]
            silence = seg.start - prev[-1].end
            span = seg.start - prev[0].start
            if silence < gap_seconds and span < max_span_seconds:
                prev.append(seg)
                continue
        groups.append([seg])
    return groups


def _write_markdown(
    segments: list[ProcessedSegment],
    video_id: str,
    url: str,
    title: str,
    recording_date: str,
    transcribed_at: str,
    audio_separation_enabled: bool,
    cfg: AppConfig,
    low_conf_count: int,
    output_dir: Path,
    source_type: str = "youtube",
) -> None:
    is_local = source_type == "local"
    groups = group_segments(
        segments,
        max_span_seconds=cfg.output.timestamp_interval_seconds,
        gap_seconds=cfg.output.paragraph_gap_seconds,
    )

    total_seconds = segments[-1].end if segments else 0.0
    char_count = sum(len(s.text) for s in segments)
    silence_suspect = sum(1 for s in segments if s.important_term_hit and s.no_speech_prob > 0.5)
    compressed = sum(1 for s in segments if s.original_text is not None)

    lines: list[str] = [
        f"# {title}",
        "",
        "| | |",
        "|---|---|",
        f"| 総時間 | {format_timestamp_hms(total_seconds)} |",
    ]
    if not is_local:
        lines.append(f"| 動画 | {url} |")
        lines.append(f"| 録画日 | {recording_date}（YouTube公開日） |")
    lines.append(f"| 文字起こし日 | {transcribed_at} |")
    lines.append(f"| 文字数 | 約 {char_count:,} 字（{len(groups)} 区切り） |")

    review_parts = [f"低信頼 {low_conf_count}"]
    if silence_suspect:
        review_parts.append(f"無音疑い {silence_suspect}")
    if compressed:
        review_parts.append(f"繰り返し圧縮 {compressed}")
    lines.append(f"| 要確認 | {'・'.join(review_parts)} 箇所 |")
    lines.extend(["", "各見出しの時刻は、その区切りで実際に話し始めた時刻。", "", "---", ""])

    for group in groups:
        ts_label = format_timestamp_hms(group[0].start)
        if is_local:
            lines.append(f"## {ts_label}")
        else:
            ts_link = youtube_url_with_timestamp(video_id, group[0].start)
            lines.append(f"## [{ts_label}]({ts_link})")
        lines.append("")

        for seg in group:
            if seg.original_text is not None:
                lines.append(seg.text)
            elif seg.low_confidence:
                lines.append(f"⚠️[要確認: 低信頼] {seg.text}")
            elif seg.important_term_hit and seg.no_speech_prob > 0.5:
                lines.append(f"⚠️[要注意: 無音疑い] {seg.text}")
            else:
                lines.append(seg.text)

        lines.append("")

    out_path = output_dir / "transcript.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    logger.debug(f"Markdown 書き出し: {out_path}")


def _write_segments_json(
    segments: list[ProcessedSegment],
    video_id: str,
    url: str,
    title: str,
    transcribed_at: str,
    audio_separation_enabled: bool,
    cfg: AppConfig,
    output_dir: Path,
    source_type: str = "youtube",
) -> None:
    is_local = source_type == "local"

    def _seg_dict(seg: ProcessedSegment) -> dict:
        d: dict = {
            "start": seg.start,
            "end": seg.end,
            "text": seg.text,
            **({"original_text": seg.original_text} if seg.original_text is not None else {}),
            "avg_logprob": seg.avg_logprob,
            "no_speech_prob": seg.no_speech_prob,
            "low_confidence": seg.low_confidence,
            "important_term_hit": seg.important_term_hit,
        }
        if not is_local:
            d["youtube_link"] = youtube_url_with_timestamp(video_id, seg.start)
        return d

    data = {
        "video_id": video_id,
        "source_type": source_type,
        "url": url,
        "title": title,
        "transcribed_at": transcribed_at,
        "config": {
            "model": cfg.transcription.model,
            "audio_separation": audio_separation_enabled,
        },
        "segments": [_seg_dict(seg) for seg in segments],
    }
    if is_local:
        data["source_path"] = url

    out_path = output_dir / "segments.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.debug(f"segments.json 書き出し: {out_path}")


def _write_meta_json(
    video_id: str,
    url: str,
    title: str,
    recording_date: str,
    transcribed_at: str,
    audio_separation_enabled: bool,
    cfg: AppConfig,
    low_conf_count: int,
    segment_count: int,
    output_dir: Path,
    source_type: str = "youtube",
    duration_seconds: float = 0.0,
) -> None:
    data: dict = {
        "video_id": video_id,
        "source_type": source_type,
        "url": url,
        "title": title,
        "recording_date": recording_date,
        "transcribed_at": transcribed_at,
        "segment_count": segment_count,
        "low_confidence_count": low_conf_count,
        "duration_minutes": round(duration_seconds / 60),
        "config_snapshot": {
            "model": cfg.transcription.model,
            "compute_type": cfg.transcription.compute_type,
            "device": cfg.transcription.device,
            "language": cfg.transcription.language,
            "beam_size": cfg.transcription.beam_size,
            "vad_filter": cfg.transcription.vad_filter,
            "audio_separation": audio_separation_enabled,
            "audio_separation_model": cfg.audio_separation.model if audio_separation_enabled else None,
            "confidence_threshold": cfg.output.confidence_threshold,
        },
    }
    if source_type == "local":
        data["source_path"] = url
        data["filename"] = Path(url).name

    out_path = output_dir / "meta.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.debug(f"meta.json 書き出し: {out_path}")
