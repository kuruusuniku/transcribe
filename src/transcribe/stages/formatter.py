from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

from ..config import AppConfig
from ..postprocess import ProcessedSegment
from ..utils import format_timestamp, youtube_url_with_timestamp

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
) -> None:
    interval = cfg.output.timestamp_interval_seconds
    sep_label = "有効" if audio_separation_enabled else "無効"

    lines: list[str] = [
        f"# {title}",
        "",
        "| | |",
        "|---|---|",
        f"| 動画 | {url} |",
        f"| 録画日 | {recording_date}（YouTube公開日） |",
        f"| 文字起こし日 | {transcribed_at} |",
        f"| 音声分離 | {sep_label} |",
        f"| モデル | faster-whisper {cfg.transcription.model} ({cfg.transcription.compute_type}) |",
        f"| 要確認セグメント数 | {low_conf_count}箇所 |",
        "",
        "---",
        "",
    ]

    # セグメントを1分ごとのバケットに振り分ける
    buckets: dict[int, list[ProcessedSegment]] = {}
    for seg in segments:
        bucket_key = int(seg.start // interval) * interval
        buckets.setdefault(bucket_key, []).append(seg)

    for bucket_start in sorted(buckets.keys()):
        ts_label = format_timestamp(bucket_start)
        ts_link = youtube_url_with_timestamp(video_id, bucket_start)
        lines.append(f"## [{ts_label}]({ts_link})")
        lines.append("")

        for seg in buckets[bucket_start]:
            text = seg.text
            if seg.low_confidence:
                text = f"⚠️[要確認: 低信頼] {text}"
            elif seg.important_term_hit and seg.no_speech_prob > 0.5:
                text = f"⚠️[要注意: 無音疑い] {text}"
            lines.append(text)

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
) -> None:
    data = {
        "video_id": video_id,
        "url": url,
        "title": title,
        "transcribed_at": transcribed_at,
        "config": {
            "model": cfg.transcription.model,
            "audio_separation": audio_separation_enabled,
        },
        "segments": [
            {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "avg_logprob": seg.avg_logprob,
                "no_speech_prob": seg.no_speech_prob,
                "low_confidence": seg.low_confidence,
                "youtube_link": youtube_url_with_timestamp(video_id, seg.start),
            }
            for seg in segments
        ],
    }
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
) -> None:
    data = {
        "video_id": video_id,
        "url": url,
        "title": title,
        "recording_date": recording_date,
        "transcribed_at": transcribed_at,
        "segment_count": segment_count,
        "low_confidence_count": low_conf_count,
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
    out_path = output_dir / "meta.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.debug(f"meta.json 書き出し: {out_path}")
