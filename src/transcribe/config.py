from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .secrets import get_secret

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
    timestamp_interval_seconds: int = 30  # 1 区切りの最大の長さ（これを超えたら次の発話から区切る）
    paragraph_gap_seconds: float = 1.0    # この長さ以上の無音があれば区切る
    segment_timestamps: bool = False      # 各発言の先頭にも実際の開始時刻を付ける
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
    retention_days: int = 30  # これより古いログファイルは起動時に削除


@dataclass
class GoogleDocsConfig:
    enabled: bool = False
    credentials_path: Path = field(default_factory=lambda: Path("credentials.json"))
    token_path: Path = field(default_factory=lambda: Path("token.json"))
    root_folder_id: str = ""


@dataclass
class SummarizeConfig:
    enabled: bool = False
    provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5-20251001"
    max_output_tokens: int = 16384
    temperature: float = 0.3

    def resolve_api_key(self) -> str:
        if self.provider == "gemini":
            return get_secret("gemini_api_key", self.gemini_api_key)
        if self.provider == "claude":
            return get_secret("anthropic_api_key", self.anthropic_api_key)
        return ""


@dataclass
class NotionConfig:
    enabled: bool = False
    token: str = ""
    database_id: str = ""
    local_database_id: str = ""

    def resolve_token(self) -> str:
        return get_secret("notion_token", self.token)


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    token: str = ""

    def resolve_token(self) -> str:
        return get_secret("web_token", self.token)


@dataclass
class NotificationConfig:
    enabled: bool = False
    to_email: str = ""


@dataclass
class AppConfig:
    paths: PathsConfig
    youtube: YoutubeConfig
    audio_separation: AudioSeparationConfig
    transcription: TranscriptionConfig
    output: OutputConfig
    retry: RetryConfig
    logging: LoggingConfig
    google_docs: GoogleDocsConfig = field(default_factory=GoogleDocsConfig)
    summarize: SummarizeConfig = field(default_factory=SummarizeConfig)
    notion: NotionConfig = field(default_factory=NotionConfig)
    web: WebConfig = field(default_factory=WebConfig)
    notification: NotificationConfig = field(default_factory=NotificationConfig)

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
        timestamp_interval_seconds=out_raw.get("timestamp_interval_seconds", 30),
        paragraph_gap_seconds=out_raw.get("paragraph_gap_seconds", 1.0),
        segment_timestamps=out_raw.get("segment_timestamps", False),
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
        retention_days=log_raw.get("retention_days", 30),
    )

    gd_raw = raw.get("google_docs", {})
    google_docs = GoogleDocsConfig(
        enabled=gd_raw.get("enabled", False),
        credentials_path=_resolve_path(gd_raw.get("credentials_path", "credentials.json"), base),
        token_path=_resolve_path(gd_raw.get("token_path", "token.json"), base),
        root_folder_id=gd_raw.get("root_folder_id", ""),
    )

    sum_raw = raw.get("summarize", {})
    summarize = SummarizeConfig(
        enabled=sum_raw.get("enabled", False),
        provider=sum_raw.get("provider", "gemini"),
        gemini_api_key=sum_raw.get("gemini_api_key", ""),
        gemini_model=sum_raw.get("gemini_model", "gemini-2.5-flash"),
        anthropic_api_key=sum_raw.get("anthropic_api_key", ""),
        anthropic_model=sum_raw.get("anthropic_model", "claude-haiku-4-5-20251001"),
        max_output_tokens=sum_raw.get("max_output_tokens", 16384),
        temperature=sum_raw.get("temperature", 0.3),
    )

    notion_raw = raw.get("notion", {})
    notion = NotionConfig(
        enabled=notion_raw.get("enabled", False),
        token=notion_raw.get("token", ""),
        database_id=notion_raw.get("database_id", ""),
        local_database_id=notion_raw.get("local_database_id", ""),
    )

    web_raw = raw.get("web", {})
    web = WebConfig(
        host=web_raw.get("host", "127.0.0.1"),
        port=web_raw.get("port", 8000),
        token=web_raw.get("token", "") or "",
    )

    notif_raw = raw.get("notification", {})
    notification = NotificationConfig(
        enabled=notif_raw.get("enabled", False),
        to_email=notif_raw.get("to_email", ""),
    )

    return AppConfig(
        paths=paths,
        youtube=youtube,
        audio_separation=sep,
        transcription=transcription,
        output=output,
        retry=retry,
        logging=log_cfg,
        google_docs=google_docs,
        summarize=summarize,
        notion=notion,
        web=web,
        notification=notification,
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
