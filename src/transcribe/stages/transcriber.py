from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generator

from ..config import AppConfig, GlossaryConfig

logger = logging.getLogger(__name__)


@dataclass
class Segment:
    start: float
    end: float
    text: str
    avg_logprob: float
    no_speech_prob: float


def transcribe(
    audio_path: Path,
    cfg: AppConfig,
    glossary: GlossaryConfig,
    model_cache: dict,
    on_progress: Callable[[float], None] | None = None,
) -> Generator[Segment, None, None]:
    """
    音声ファイルを文字起こしし、Segment をジェネレータで yield する。
    model_cache に {"model": WhisperModel} を持たせることで複数ジョブ間でモデルを再利用する。
    on_progress には処理済みの割合（0.0〜1.0）が逐次渡される。
    """
    from faster_whisper import WhisperModel  # noqa: PLC0415

    if "model" not in model_cache:
        model_name = cfg.transcription.model
        compute_type = cfg.transcription.compute_type
        device = cfg.transcription.device
        logger.info(
            f"Whisper モデルロード: {model_name}  compute_type={compute_type}  device={device}"
        )
        model_cache["model"] = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
        )

    model: WhisperModel = model_cache["model"]

    initial_prompt = glossary.context.strip() or None
    vad_p = cfg.transcription.vad_parameters
    vad_params = {
        "min_silence_duration_ms": vad_p.min_silence_duration_ms,
        "threshold": vad_p.threshold,
        "speech_pad_ms": vad_p.speech_pad_ms,
    }

    logger.info(
        f"文字起こし開始: {audio_path.name}  "
        f"condition_on_previous_text={cfg.transcription.condition_on_previous_text}  "
        f"vad={vad_params}"
    )
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=cfg.transcription.language,
        beam_size=cfg.transcription.beam_size,
        condition_on_previous_text=cfg.transcription.condition_on_previous_text,
        vad_filter=cfg.transcription.vad_filter,
        vad_parameters=vad_params,
        initial_prompt=initial_prompt,
    )

    logger.debug(
        f"検出言語: {info.language}  確率: {info.language_probability:.2f}  "
        f"推定音声時間: {info.duration:.1f}s"
    )

    duration = info.duration or 0.0
    for seg in segments_iter:
        if on_progress is not None and duration > 0:
            on_progress(min(seg.end / duration, 1.0))
        yield Segment(
            start=seg.start,
            end=seg.end,
            text=seg.text.strip(),
            avg_logprob=seg.avg_logprob,
            no_speech_prob=seg.no_speech_prob,
        )
