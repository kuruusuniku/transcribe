from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class PathsConfig:
    work_dir: Path
    output_dir: Path
    state_db: Path
    log_dir: Path


@dataclass
class YoutubeConfig:
    cookies_from_browser: str = "chrome"


@dataclass
class AudioSeparationConfig:
    enabled: bool = False
    model: str = "htdemucs"
    device: str = "cuda"


@dataclass
class VadParameters:
    min_silence_duration_ms: int = 1000
    threshold: float = 0.5
    speech_pad_ms: int = 200


@dataclass
class TranscriptionConfig:
    model: str = "large-v3"
    compute_type: str = "int8_float16"
    device: str = "cuda"
    language: str = "ja"
    beam_size: int = 5
    condition_on_previous_text: bool = False
    vad_filter: bool = True
    vad_parameters: VadParameters = field(default_factory=VadParameters)


@dataclass
class OutputConfig:
    timestamp_interval_seconds: int = 60
    confidence_threshold: float = -1.0


@dataclass
class RetryConfig:
    max_attempts: int = 3
    backoff_seconds: list[int] = field(default_factory=lambda: [60, 300, 900])


@dataclass
class LoggingConfig:
    level: str = "INFO"
    console: bool = True
    file: bool = True


@dataclass
class AppConfig:
    paths: PathsConfig
    youtube: YoutubeConfig
    audio_separation: AudioSeparationConfig
    transcription: TranscriptionConfig
    output: OutputConfig
    retry: RetryConfig
    logging: LoggingConfig

    @property
    def work_dir(self) -> Path:
        return self.paths.work_dir

    @property
    def output_dir(self) -> Path:
        return self.paths.output_dir

    @property
    def state_db(self) -> Path:
        return self.paths.state_db

    @property
    def log_dir(self) -> Path:
        return self.paths.log_dir


def _resolve_path(raw: str, base: Path) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = (base / p).resolve()
    return p


def load_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        logger.error(f"設定ファイルが見つかりません: {config_path}")
        sys.exit(1)

    with config_path.open(encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f)

    base = config_path.parent

    paths_raw = raw.get("paths", {})
    paths = PathsConfig(
        work_dir=_resolve_path(paths_raw.get("work_dir", "./data/work"), base),
        output_dir=_resolve_path(paths_raw.get("output_dir", "./data/output"), base),
        state_db=_resolve_path(paths_raw.get("state_db", "./data/state.db"), base),
        log_dir=_resolve_path(paths_raw.get("log_dir", "./logs"), base),
    )

    yt_raw = raw.get("youtube", {})
    youtube = YoutubeConfig(
        cookies_from_browser=yt_raw.get("cookies_from_browser", "chrome"),
    )

    sep_raw = raw.get("audio_separation", {})
    vad_raw = raw.get("transcription", {}).get("vad_parameters", {})
    tr_raw = raw.get("transcription", {})
    transcription = TranscriptionConfig(
        model=tr_raw.get("model", "large-v3"),
        compute_type=tr_raw.get("compute_type", "int8_float16"),
        device=tr_raw.get("device", "cuda"),
        language=tr_raw.get("language", "ja"),
        beam_size=tr_raw.get("beam_size", 5),
        vad_filter=tr_raw.get("vad_filter", True),
        condition_on_previous_text=tr_raw.get("condition_on_previous_text", False),
        vad_parameters=VadParameters(
            min_silence_duration_ms=vad_raw.get("min_silence_duration_ms", 1000),
            threshold=vad_raw.get("threshold", 0.5),
            speech_pad_ms=vad_raw.get("speech_pad_ms", 200),
        ),
    )

    sep = AudioSeparationConfig(
        enabled=sep_raw.get("enabled", False),
        model=sep_raw.get("model", "htdemucs"),
        device=sep_raw.get("device", "cuda"),
    )

    out_raw = raw.get("output", {})
    output = OutputConfig(
        timestamp_interval_seconds=out_raw.get("timestamp_interval_seconds", 60),
        confidence_threshold=out_raw.get("confidence_threshold", -1.0),
    )

    retry_raw = raw.get("retry", {})
    retry = RetryConfig(
        max_attempts=retry_raw.get("max_attempts", 3),
        backoff_seconds=retry_raw.get("backoff_seconds", [60, 300, 900]),
    )

    log_raw = raw.get("logging", {})
    log_cfg = LoggingConfig(
        level=log_raw.get("level", "INFO"),
        console=log_raw.get("console", True),
        file=log_raw.get("file", True),
    )

    return AppConfig(
        paths=paths,
        youtube=youtube,
        audio_separation=sep,
        transcription=transcription,
        output=output,
        retry=retry,
        logging=log_cfg,
    )


@dataclass
class GlossaryConfig:
    context: str = ""
    substitutions: list[dict] = field(default_factory=list)
    important_terms: list[str] = field(default_factory=list)


def load_glossary(glossary_path: Path) -> GlossaryConfig:
    if not glossary_path.exists():
        logger.warning(f"用語辞書が見つかりません: {glossary_path}")
        return GlossaryConfig()

    with glossary_path.open(encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    subs = raw.get("substitutions", []) or []

    return GlossaryConfig(
        context=raw.get("context", ""),
        substitutions=subs,
        important_terms=raw.get("important_terms", []),
    )
