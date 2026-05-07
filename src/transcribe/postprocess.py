from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Generator, Iterable

from .config import AppConfig, GlossaryConfig
from .stages.transcriber import Segment


@dataclass
class ProcessedSegment:
    start: float
    end: float
    text: str
    avg_logprob: float
    no_speech_prob: float
    low_confidence: bool
    important_term_hit: bool


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
