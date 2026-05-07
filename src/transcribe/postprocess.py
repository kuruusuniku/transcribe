from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Generator, Iterable

from .config import AppConfig, GlossaryConfig
from .stages.transcriber import Segment

_NORMALIZE_RE = re.compile(r"[\s　、。，．！？!?,.・・]+")


def _normalize(text: str) -> str:
    """句読点・空白を除去して比較用の正規化文字列を返す。"""
    return _NORMALIZE_RE.sub("", text).strip()


@dataclass
class ProcessedSegment:
    start: float
    end: float
    text: str
    avg_logprob: float
    no_speech_prob: float
    low_confidence: bool
    important_term_hit: bool
    original_text: str | None = field(default=None)  # 圧縮時のみ設定


def postprocess(
    segments: Iterable[Segment],
    cfg: AppConfig,
    glossary: GlossaryConfig,
) -> Generator[ProcessedSegment, None, None]:
    """Whisper セグメントをストリームで受け取り、後処理済みセグメントを yield する。"""
    threshold = cfg.output.confidence_threshold

    for seg in segments:
        text = seg.text

        # 用語置換
        for rule in glossary.substitutions:
            pattern = rule.get("pattern", "")
            replacement = rule.get("replacement", "")
            rule_type = rule.get("type", "literal")
            if not pattern:
                continue
            if rule_type == "regex":
                text = re.sub(pattern, replacement, text)
            else:
                text = text.replace(pattern, replacement)

        # 信頼度判定
        low_conf = seg.avg_logprob < threshold

        # 重要語ヒット判定
        important_hit = any(term in text for term in glossary.important_terms)

        yield ProcessedSegment(
            start=seg.start,
            end=seg.end,
            text=text,
            avg_logprob=seg.avg_logprob,
            no_speech_prob=seg.no_speech_prob,
            low_confidence=low_conf,
            important_term_hit=important_hit,
        )


def compress_repetitions(
    segments: Iterable[ProcessedSegment],
    min_repeat: int = 3,
) -> Generator[ProcessedSegment, None, None]:
    """連続する同一テキスト(min_repeat回以上)を検出し、最初の1つを残して後続を圧縮マークに置換する。

    圧縮されたセグメントは low_confidence=True, original_text=元テキスト になる。
    """
    buffer: list[ProcessedSegment] = []

    def _flush(buf: list[ProcessedSegment]) -> Generator[ProcessedSegment, None, None]:
        if not buf:
            return
        if len(buf) >= min_repeat:
            yield buf[0]
            marker = f"⚠️[同一フレーズ {len(buf)}回繰り返しを検出（自動圧縮）]"
            for seg in buf[1:]:
                yield ProcessedSegment(
                    start=seg.start,
                    end=seg.end,
                    text=marker,
                    avg_logprob=seg.avg_logprob,
                    no_speech_prob=seg.no_speech_prob,
                    low_confidence=True,
                    important_term_hit=False,
                    original_text=seg.text,
                )
        else:
            yield from buf

    for seg in segments:
        if buffer and _normalize(buffer[-1].text) == _normalize(seg.text):
            buffer.append(seg)
        else:
            yield from _flush(buffer)
            buffer = [seg]

    yield from _flush(buffer)
