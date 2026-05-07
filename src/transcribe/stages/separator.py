from __future__ import annotations

import gc
import logging
from pathlib import Path

from ..config import AppConfig

logger = logging.getLogger(__name__)


def separate_audio(audio_path: Path, work_dir: Path, cfg: AppConfig) -> Path:
    """
    Demucs でボーカル（音声）を分離して返す。
    audio_separation.enabled=False の場合は入力をそのまま返す。
    """
    if not cfg.audio_separation.enabled:
        logger.debug("音声分離スキップ（無効）")
        return audio_path

    import torch  # noqa: PLC0415
    import demucs.api  # noqa: PLC0415

    model_name = cfg.audio_separation.model
    device = cfg.audio_separation.device
    logger.info(f"Demucs 音声分離開始: model={model_name}  device={device}")

    separator = demucs.api.Separator(model=model_name, device=device)
    _, separated = separator.separate_audio_file(audio_path)

    # ボーカルトラック（vocals）を取り出す
    vocals_key = "vocals"
    if vocals_key not in separated:
        available = list(separated.keys())
        raise KeyError(f"Demucs の出力に 'vocals' がありません。利用可能: {available}")

    vocals_tensor = separated[vocals_key]
    sample_rate = separator.samplerate

    out_path = work_dir / f"{audio_path.stem}_vocals.wav"

    import torchaudio  # noqa: PLC0415

    torchaudio.save(str(out_path), vocals_tensor.cpu(), sample_rate)
    logger.info(f"音声分離完了 → {out_path.name}")

    # VRAM 解放（Whisperと同時ロード禁止）
    del separator, separated, vocals_tensor
    gc.collect()
    torch.cuda.empty_cache()
    logger.debug("Demucs VRAM 解放完了")

    return out_path
