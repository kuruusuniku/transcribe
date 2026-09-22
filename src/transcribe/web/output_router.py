"""スレッドごとに出力先を切り替える stdout / stderr ラッパー。

Web サーバーでは CLI コマンドをワーカースレッドでインプロセス実行する。
そのスレッドの出力（rich の console.print・ログ・ライブラリの print / tqdm）だけを
タスクのログストリームに流し、他スレッド（uvicorn 等）の出力は元のストリームに出す。
サブプロセスの stdout をパイプで読んでいた従来の挙動と同じ内容がタスクログに届く。
"""

from __future__ import annotations

import re
import sys
import threading
from typing import Callable, TextIO

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_local = threading.local()


class _RoutedStream:
    def __init__(self, fallback: TextIO) -> None:
        self._fallback = fallback

    def _sink(self) -> Callable[[str], None] | None:
        return getattr(_local, "sink", None)

    def write(self, text: str) -> int:
        sink = self._sink()
        if sink is None:
            return self._fallback.write(text)
        sink(text)
        return len(text)

    def flush(self) -> None:
        if self._sink() is None:
            self._fallback.flush()

    def isatty(self) -> bool:
        return False if self._sink() is not None else self._fallback.isatty()

    def __getattr__(self, name: str):
        return getattr(self._fallback, name)


_installed = False


def install() -> None:
    """sys.stdout / sys.stderr をルーティング可能なストリームに差し替える（プロセスで 1 回）。"""
    global _installed
    if _installed:
        return
    sys.stdout = _RoutedStream(sys.stdout)  # type: ignore[assignment]
    sys.stderr = _RoutedStream(sys.stderr)  # type: ignore[assignment]
    _installed = True


class LineCapture:
    """現在のスレッドの出力を行単位で emit に渡すコンテキスト。"""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._buf = ""

    def _write(self, text: str) -> None:
        self._buf += _ANSI_RE.sub("", text)
        # \r は進捗バーの上書き更新。行区切りとして扱う
        *lines, self._buf = re.split(r"\r\n|\n|\r", self._buf)
        for line in lines:
            if line.strip():
                self._emit(line.rstrip())

    def __enter__(self) -> "LineCapture":
        _local.sink = self._write
        return self

    def __exit__(self, *exc) -> None:
        _local.sink = None
        if self._buf.strip():
            self._emit(self._buf.rstrip())
        self._buf = ""
