from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

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
) -> Generator[Segment, None, None]:
    """
    音声ファイルを文字起こしし、Segment をジェネレータで yield する。
    model_cache に {"model": WhisperModel} を持たせることで複数ジョブ間でモデルを再利用する。
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
    vad_params = {
        "min_silence_duration_ms": cfg.transcription.vad_parameters.min_silence_duration_ms,
    }

    logger.info(f"文字起こし開始: {audio_path.name}")
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=cfg.transcription.language,
        beam_size=cfg.transcription.beam_size,
        vad_filter=cfg.transcription.vad_filter,
        vad_parameters=vad_params,
        initial_prompt=initial_prompt,
    )

    logger.debug(
        f"検出言語: {info.language}  確率: {info.language_probability:.2f}  "
        f"推定音声時間: {info.duration:.1f}s"
    )

    for seg in segments_iter:
        yield Segment(
            start=seg.start,
            end=seg.end,
            text=seg.text.strip(),
            avg_logprob=seg.avg_logprob,
            no_speech_prob=seg.no_speech_prob,
        )
