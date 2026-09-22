"""transcript.md の区切り方とヘッダーのテスト。"""
from __future__ import annotations

import json

import pytest

from transcribe.config import (
    AppConfig,
    AudioSeparationConfig,
    GoogleDocsConfig,
    LoggingConfig,
    OutputConfig,
    PathsConfig,
    RetryConfig,
    SummarizeConfig,
    TranscriptionConfig,
    VadParameters,
    YoutubeConfig,
)
from transcribe.postprocess import ProcessedSegment
from transcribe.stages.formatter import format_outputs, group_segments, rebuild_outputs


def _seg(start, end, text, **kw):
    return ProcessedSegment(
        start=start, end=end, text=text,
        avg_logprob=kw.get("avg_logprob", -0.2),
        no_speech_prob=kw.get("no_speech_prob", 0.0),
        low_confidence=kw.get("low_confidence", False),
        important_term_hit=kw.get("important_term_hit", False),
        original_text=kw.get("original_text"),
    )


@pytest.fixture
def cfg(tmp_path):
    return AppConfig(
        paths=PathsConfig(
            work_dir=tmp_path / "work", output_dir=tmp_path / "out",
            state_db=tmp_path / "state.db", log_dir=tmp_path / "logs",
        ),
        youtube=YoutubeConfig(),
        audio_separation=AudioSeparationConfig(),
        transcription=TranscriptionConfig(vad_parameters=VadParameters()),
        output=OutputConfig(timestamp_interval_seconds=60, paragraph_gap_seconds=2.0),
        retry=RetryConfig(),
        logging=LoggingConfig(),
        google_docs=GoogleDocsConfig(),
        summarize=SummarizeConfig(),
    )


# ─── group_segments ───────────────────────────────────────────────────────


def test_groups_split_on_silence():
    segs = [_seg(0, 3, "a"), _seg(3.5, 6, "b"), _seg(12.0, 14, "c")]
    groups = group_segments(segs, max_span_seconds=600, gap_seconds=2.0)
    assert [[s.text for s in g] for g in groups] == [["a", "b"], ["c"]]
    # 見出しの時刻は実際の発話開始時刻
    assert [g[0].start for g in groups] == [0, 12.0]


def test_groups_split_when_span_exceeds_max():
    segs = [_seg(i, i + 0.9, str(i)) for i in range(0, 100)]  # 無音なしで 100 秒
    groups = group_segments(segs, max_span_seconds=60, gap_seconds=2.0)
    assert len(groups) == 2
    assert groups[1][0].start == 60  # 上限を超えた次の発話から新しい区切り


# ─── transcript.md ────────────────────────────────────────────────────────


def test_markdown_heading_uses_actual_speech_start(cfg, tmp_path):
    out = tmp_path / "job"
    segs = [_seg(0, 3, "はじめ"), _seg(7.5, 9, "つぎ")]
    format_outputs(
        segments=segs, video_id="vid12345678", url="https://www.youtube.com/watch?v=vid12345678",
        title="テスト", upload_date="20250406", audio_separation_enabled=False,
        cfg=cfg, output_dir=out, source_type="youtube",
    )
    text = (out / "transcript.md").read_text(encoding="utf-8")

    assert "## [00:00:00](https://www.youtube.com/watch?v=vid12345678&t=0s)" in text
    assert "## [00:00:07](https://www.youtube.com/watch?v=vid12345678&t=7s)" in text
    # 実用的でない情報は載せない
    assert "モデル" not in text
    assert "音声分離" not in text
    # 実用的な情報
    assert "| 総時間 | 00:00:09 |" in text
    assert "文字数" in text and "要確認" in text


def test_markdown_for_local_has_no_path_or_link(cfg, tmp_path):
    out = tmp_path / "job"
    format_outputs(
        segments=[_seg(0, 2, "講義")], video_id="lecture_ab12cd34", url="C:/audio/講義.mp3",
        title="講義.mp3", upload_date=None, audio_separation_enabled=False,
        cfg=cfg, output_dir=out, source_type="local",
    )
    text = (out / "transcript.md").read_text(encoding="utf-8")

    assert "C:/audio/講義.mp3" not in text  # ファイルパスは載せない
    assert "## 00:00:00" in text


# ─── rebuild_outputs ──────────────────────────────────────────────────────


def test_rebuild_outputs_regenerates_transcript(cfg, tmp_path):
    out = tmp_path / "job"
    format_outputs(
        segments=[_seg(0, 3, "はじめ"), _seg(30, 33, "つぎ")], video_id="vid12345678",
        url="https://www.youtube.com/watch?v=vid12345678", title="テスト",
        upload_date="20250406", audio_separation_enabled=False, cfg=cfg, output_dir=out,
    )
    (out / "transcript.md").write_text("壊れた内容", encoding="utf-8")

    count = rebuild_outputs(out, cfg)

    assert count == 2
    text = (out / "transcript.md").read_text(encoding="utf-8")
    assert "## [00:00:30]" in text
    assert json.loads((out / "meta.json").read_text(encoding="utf-8"))["recording_date"] == "2025-04-06"
