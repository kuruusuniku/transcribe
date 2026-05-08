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
    import torchaudio  # noqa: PLC0415
    from demucs.apply import apply_model  # noqa: PLC0415
    from demucs.audio import convert_audio  # noqa: PLC0415
    from demucs.pretrained import get_model  # noqa: PLC0415

    model_name = cfg.audio_separation.model
    device = cfg.audio_separation.device
    logger.info(f"Demucs 音声分離開始: model={model_name}  device={device}")

    model = get_model(name=model_name)
    model.to(device)
    model.eval()

    wav = sources = vocals = None
    try:
        wav, sr = torchaudio.load(str(audio_path))
        wav = convert_audio(wav, sr, model.samplerate, model.audio_channels)
        wav = wav.to(device)

        with torch.no_grad():
            sources = apply_model(
                model,
                wav[None],
                device=device,
                progress=True,
            )

        sources = sources[0]
        vocals_idx = model.sources.index("vocals")
        vocals = sources[vocals_idx].cpu()

        out_path = work_dir / f"{audio_path.stem}_vocals.wav"
        torchaudio.save(str(out_path), vocals, model.samplerate)
        logger.info(f"音声分離完了 → {out_path.name}")

        return out_path

    finally:
        del model
        if wav is not None:
            del wav
        if sources is not None:
            del sources
        if vocals is not None:
            del vocals
        gc.collect()
        if device == "cuda":
            torch.cuda.empty_cache()
        logger.debug("Demucs VRAM 解放完了")
