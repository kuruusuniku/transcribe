from __future__ import annotations

import pytest

from transcribe.utils import normalize_youtube_url

CANONICAL = "https://www.youtube.com/watch?v=SAElLAs0GY0"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=SAElLAs0GY0",
        "https://youtube.com/watch?v=SAElLAs0GY0&t=120s",
        "https://m.youtube.com/watch?v=SAElLAs0GY0&list=PL123&index=2",
        "https://youtu.be/SAElLAs0GY0",
        "https://youtu.be/SAElLAs0GY0?si=abc&t=10",
        "https://www.youtube.com/shorts/SAElLAs0GY0",
        "https://www.youtube.com/live/SAElLAs0GY0?feature=share",
        " https://www.youtube.com/embed/SAElLAs0GY0 ",
    ],
)
def test_normalize_variants(url):
    assert normalize_youtube_url(url) == CANONICAL


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/watch?v=SAElLAs0GY0",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/watch?v=short",
    ],
)
def test_non_video_urls_unchanged(url):
    assert normalize_youtube_url(url) == url


def test_format_timestamp_hms_always_has_hours():
    from transcribe.utils import format_timestamp_hms

    assert format_timestamp_hms(0) == "00:00:00"
    assert format_timestamp_hms(300) == "00:05:00"
    assert format_timestamp_hms(3725) == "01:02:05"
