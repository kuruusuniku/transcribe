"""文字起こしの高速化テスト: モデルと beam_size の組み合わせを比較する。

使い方:
    uv run python scripts/bench_transcribe.py <音声パス> <切り出し秒数> <設定名...>

設定名: base / base-beam1 / turbo / turbo-beam1
結果は data/bench/ に出力する（results.json と設定名ごとの書き起こし）。
"""
from __future__ import annotations

import difflib
import json
import subprocess
import sys
import time
from pathlib import Path

OUT = Path("data/bench")

CONFIGS = {
    "base": ("large-v3", 5),
    "base-beam1": ("large-v3", 1),
    "turbo": ("large-v3-turbo", 5),
    "turbo-beam1": ("large-v3-turbo", 1),
}


def slice_audio(src: Path, seconds: int) -> Path:
    """先頭 N 秒を 16kHz モノラルに切り出す（毎回同じ条件で比較するため）。"""
    dst = OUT / f"clip_{seconds}s.wav"
    if dst.exists():
        return dst
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-t", str(seconds), "-ac", "1", "-ar", "16000", str(dst)],
        check=True, capture_output=True,
    )
    return dst


def run_config(name: str, clip: Path, context: str) -> dict:
    from faster_whisper import WhisperModel

    model_name, beam = CONFIGS[name]
    t0 = time.time()
    model = WhisperModel(model_name, device="cuda", compute_type="int8")
    load_s = time.time() - t0

    t1 = time.time()
    segments, _info = model.transcribe(
        str(clip), language="ja", beam_size=beam,
        condition_on_previous_text=False, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 1000, "threshold": 0.5, "speech_pad_ms": 200},
        initial_prompt=context or None,
    )
    texts = [s.text.strip() for s in segments]
    transcribe_s = time.time() - t1

    (OUT / f"{name}.txt").write_text("\n".join(texts), encoding="utf-8")
    del model
    return {
        "config": name, "model": model_name, "beam": beam,
        "load_s": round(load_s, 1), "transcribe_s": round(transcribe_s, 1),
        "segments": len(texts), "chars": sum(len(t) for t in texts),
    }


def main() -> None:
    src = Path(sys.argv[1])
    seconds = int(sys.argv[2])
    names = sys.argv[3:] or list(CONFIGS)

    import yaml

    OUT.mkdir(parents=True, exist_ok=True)
    glossary = yaml.safe_load(Path("glossary.yaml").read_text(encoding="utf-8"))
    context = (glossary.get("context") or "").strip()

    clip = slice_audio(src, seconds)
    results_path = OUT / "results.json"
    results = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else []

    for name in names:
        print(f"== {name} 実行中…", flush=True)
        result = run_config(name, clip, context)
        results = [r for r in results if r["config"] != name] + [result]
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        speed = seconds / result["transcribe_s"] if result["transcribe_s"] else 0
        print(f"   {json.dumps(result, ensure_ascii=False)}  実時間比 {speed:.1f}x", flush=True)

    base_file = OUT / "base.txt"
    if base_file.exists():
        base_text = base_file.read_text(encoding="utf-8")
        for name in CONFIGS:
            f = OUT / f"{name}.txt"
            if name != "base" and f.exists():
                ratio = difflib.SequenceMatcher(None, base_text, f.read_text(encoding="utf-8")).ratio()
                print(f"{name}: base（large-v3 beam5）との一致率 {ratio:.3f}", flush=True)


if __name__ == "__main__":
    main()
