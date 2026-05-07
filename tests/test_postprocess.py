"""compress_repetitions のユニットテスト"""
from __future__ import annotations

import pytest

from transcribe.postprocess import ProcessedSegment, compress_repetitions


def _seg(text: str, start: float = 0.0, end: float = 1.0) -> ProcessedSegment:
    return ProcessedSegment(
        start=start,
        end=end,
        text=text,
        avg_logprob=-0.2,
        no_speech_prob=0.01,
        low_confidence=False,
        important_term_hit=False,
    )


def _compress(segs, min_repeat=3):
    return list(compress_repetitions(segs, min_repeat=min_repeat))


class TestNoCompression:
    def test_empty(self):
        assert _compress([]) == []

    def test_single(self):
        result = _compress([_seg("A")])
        assert len(result) == 1
        assert result[0].text == "A"
        assert result[0].original_text is None

    def test_two_identical_below_threshold(self):
        result = _compress([_seg("A"), _seg("A")])
        assert len(result) == 2
        assert all(s.original_text is None for s in result)

    def test_distinct_segments(self):
        segs = [_seg("A"), _seg("B"), _seg("C")]
        result = _compress(segs)
        assert [s.text for s in result] == ["A", "B", "C"]
        assert all(s.original_text is None for s in result)


class TestCompression:
    def test_exactly_min_repeat(self):
        segs = [_seg("同じ")] * 3
        result = _compress(segs, min_repeat=3)
        assert len(result) == 3
        assert result[0].text == "同じ"
        assert result[0].original_text is None
        for s in result[1:]:
            assert "3回" in s.text
            assert s.original_text == "同じ"
            assert s.low_confidence is True

    def test_above_min_repeat(self):
        segs = [_seg("繰り返し")] * 5
        result = _compress(segs, min_repeat=3)
        assert len(result) == 5
        assert result[0].text == "繰り返し"
        for s in result[1:]:
            assert "5回" in s.text
            assert s.original_text == "繰り返し"

    def test_normalization_ignores_punctuation(self):
        """句読点の差は同一テキストとみなす"""
        segs = [_seg("こんにちは。"), _seg("こんにちは、"), _seg("こんにちは")]
        result = _compress(segs, min_repeat=3)
        assert result[0].text == "こんにちは。"
        assert result[1].original_text == "こんにちは、"
        assert result[2].original_text == "こんにちは"

    def test_compression_in_middle(self):
        """前後に別テキストがある場合、中間の連続のみ圧縮"""
        segs = [_seg("A"), _seg("B"), _seg("B"), _seg("B"), _seg("C")]
        result = _compress(segs, min_repeat=3)
        assert result[0].text == "A"
        assert result[1].text == "B"
        assert result[2].original_text == "B"
        assert result[3].original_text == "B"
        assert result[4].text == "C"

    def test_two_separate_runs(self):
        segs = [_seg("X")] * 3 + [_seg("Y")] + [_seg("Z")] * 3
        result = _compress(segs, min_repeat=3)
        assert result[0].text == "X"
        assert result[1].original_text == "X"
        assert result[2].original_text == "X"
        assert result[3].text == "Y"
        assert result[4].text == "Z"
        assert result[5].original_text == "Z"
        assert result[6].original_text == "Z"

    def test_marker_format(self):
        segs = [_seg("ループ")] * 4
        result = _compress(segs, min_repeat=3)
        assert "⚠️" in result[1].text
        assert "4回" in result[1].text
        assert "自動圧縮" in result[1].text
