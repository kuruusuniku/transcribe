"""separate_audio のユニットテスト。

実モデルは使わない:
- demucs.pretrained.get_model / demucs.apply.apply_model / demucs.audio.convert_audio は
  sys.modules に差し込んだモックモジュールで差し替える
- torchaudio.load / torchaudio.save も patch して実ファイル不要
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import torch


# ── helpers ──────────────────────────────────────────────────────────────────

SOURCES = ["drums", "bass", "other", "vocals"]
SAMPLE_RATE = 44100
AUDIO_CHANNELS = 2
N_SAMPLES = 1000


def _make_model_mock() -> MagicMock:
    m = MagicMock()
    m.samplerate = SAMPLE_RATE
    m.audio_channels = AUDIO_CHANNELS
    m.sources = SOURCES
    return m


def _make_cfg(device: str = "cuda") -> MagicMock:
    cfg = MagicMock()
    cfg.audio_separation.enabled = True
    cfg.audio_separation.model = "htdemucs"
    cfg.audio_separation.device = device
    return cfg


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def demucs_sys_mocks():
    """sys.modules に demucs サブモジュールのモックを注入し、テスト後に元に戻す。"""
    apply_mod = MagicMock()
    pretrained_mod = MagicMock()
    audio_mod = MagicMock()

    keys = ["demucs", "demucs.apply", "demucs.pretrained", "demucs.audio"]
    saved = {k: sys.modules.get(k) for k in keys}

    sys.modules["demucs"] = MagicMock()
    sys.modules["demucs.apply"] = apply_mod
    sys.modules["demucs.pretrained"] = pretrained_mod
    sys.modules["demucs.audio"] = audio_mod

    yield apply_mod, pretrained_mod, audio_mod

    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def _run_separate(audio_path, work_dir, cfg, demucs_sys_mocks):
    """共通のモック設定で separate_audio を呼び出すヘルパー。"""
    apply_mod, pretrained_mod, audio_mod = demucs_sys_mocks
    model_mock = _make_model_mock()
    pretrained_mod.get_model.return_value = model_mock

    # apply_model の戻り値: [batch=1, sources=4, channels=2, time=N]
    fake_sources = torch.zeros(1, len(SOURCES), AUDIO_CHANNELS, N_SAMPLES)
    apply_mod.apply_model.return_value = fake_sources

    # convert_audio は入力テンソルをそのまま返す
    audio_mod.convert_audio.side_effect = lambda wav, sr, tsr, ch: wav

    with patch("torchaudio.load", return_value=(torch.zeros(AUDIO_CHANNELS, N_SAMPLES), 16000)) as mock_load, \
         patch("torchaudio.save") as mock_save, \
         patch("torch.cuda.empty_cache") as mock_cache:
        from transcribe.stages.separator import separate_audio
        result = separate_audio(audio_path, work_dir, cfg)

    return result, model_mock, apply_mod, pretrained_mod, audio_mod, mock_load, mock_save, mock_cache


# ── テスト ────────────────────────────────────────────────────────────────────

class TestSeparateAudioDisabled:
    def test_returns_input_path_when_disabled(self, tmp_path):
        cfg = MagicMock()
        cfg.audio_separation.enabled = False
        audio_path = tmp_path / "input.wav"

        from transcribe.stages.separator import separate_audio
        result = separate_audio(audio_path, tmp_path, cfg)

        assert result == audio_path


class TestSeparateAudioCore:
    def test_loads_input_audio(self, tmp_path, demucs_sys_mocks):
        audio_path = tmp_path / "test_audio.wav"
        cfg = _make_cfg(device="cpu")

        result, model_mock, apply_mod, pretrained_mod, audio_mod, mock_load, mock_save, mock_cache = \
            _run_separate(audio_path, tmp_path, cfg, demucs_sys_mocks)

        mock_load.assert_called_once_with(str(audio_path))

    def test_model_moved_to_device(self, tmp_path, demucs_sys_mocks):
        audio_path = tmp_path / "test_audio.wav"
        cfg = _make_cfg(device="cpu")

        result, model_mock, *_ = _run_separate(audio_path, tmp_path, cfg, demucs_sys_mocks)

        model_mock.to.assert_called_once_with("cpu")

    def test_apply_model_called(self, tmp_path, demucs_sys_mocks):
        audio_path = tmp_path / "test_audio.wav"
        cfg = _make_cfg(device="cpu")

        result, model_mock, apply_mod, *_ = _run_separate(audio_path, tmp_path, cfg, demucs_sys_mocks)

        apply_mod.apply_model.assert_called_once()
        args, kwargs = apply_mod.apply_model.call_args
        assert args[0] is model_mock
        assert kwargs.get("device") == "cpu"

    def test_output_file_path_is_correct(self, tmp_path, demucs_sys_mocks):
        audio_path = tmp_path / "my_audio.wav"
        cfg = _make_cfg(device="cpu")

        result, *_ = _run_separate(audio_path, tmp_path, cfg, demucs_sys_mocks)

        assert result == tmp_path / "my_audio_vocals.wav"

    def test_torchaudio_save_called_with_correct_path(self, tmp_path, demucs_sys_mocks):
        audio_path = tmp_path / "my_audio.wav"
        cfg = _make_cfg(device="cpu")

        result, model_mock, apply_mod, pretrained_mod, audio_mod, mock_load, mock_save, mock_cache = \
            _run_separate(audio_path, tmp_path, cfg, demucs_sys_mocks)

        mock_save.assert_called_once()
        save_path, save_tensor, save_sr = mock_save.call_args[0]
        assert save_path == str(tmp_path / "my_audio_vocals.wav")
        assert save_sr == SAMPLE_RATE

    def test_empty_cache_called_for_cuda_device(self, tmp_path, demucs_sys_mocks):
        audio_path = tmp_path / "test_audio.wav"
        cfg = _make_cfg(device="cuda")

        *_, mock_cache = _run_separate(audio_path, tmp_path, cfg, demucs_sys_mocks)

        mock_cache.assert_called_once()

    def test_empty_cache_not_called_for_cpu_device(self, tmp_path, demucs_sys_mocks):
        audio_path = tmp_path / "test_audio.wav"
        cfg = _make_cfg(device="cpu")

        *_, mock_cache = _run_separate(audio_path, tmp_path, cfg, demucs_sys_mocks)

        mock_cache.assert_not_called()


class TestSeparateAudioFinallyOnException:
    def test_model_released_on_exception(self, tmp_path, demucs_sys_mocks):
        """apply_model が例外を投げてもモデル解放コードが実行される。"""
        apply_mod, pretrained_mod, audio_mod = demucs_sys_mocks
        model_mock = _make_model_mock()
        pretrained_mod.get_model.return_value = model_mock
        apply_mod.apply_model.side_effect = RuntimeError("GPU OOM")
        audio_mod.convert_audio.side_effect = lambda wav, sr, tsr, ch: wav

        with patch("torchaudio.load", return_value=(torch.zeros(AUDIO_CHANNELS, N_SAMPLES), 16000)), \
             patch("torchaudio.save"), \
             patch("torch.cuda.empty_cache") as mock_cache:
            from transcribe.stages.separator import separate_audio
            with pytest.raises(RuntimeError, match="GPU OOM"):
                separate_audio(tmp_path / "audio.wav", tmp_path, _make_cfg(device="cuda"))

        mock_cache.assert_called_once()

    def test_get_model_called_with_correct_name(self, tmp_path, demucs_sys_mocks):
        audio_path = tmp_path / "test_audio.wav"
        cfg = _make_cfg(device="cpu")

        result, model_mock, apply_mod, pretrained_mod, *_ = \
            _run_separate(audio_path, tmp_path, cfg, demucs_sys_mocks)

        pretrained_mod.get_model.assert_called_once_with(name="htdemucs")
