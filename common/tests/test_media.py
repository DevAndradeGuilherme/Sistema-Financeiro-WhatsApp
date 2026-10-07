import pytest

from common.media import MP3, MP4, OGG, WAV, WEBM, detect_audio_format


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"OggS" + b"\x00" * 60, OGG),
        (b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 40, MP4),
        (b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 40, WAV),
        (b"\x1a\x45\xdf\xa3" + b"\x00" * 40, WEBM),
        (b"ID3\x04" + b"\x00" * 40, MP3),
        (b"\xff\xfb\x90\x64" + b"\x00" * 40, MP3),
    ],
)
def test_detects_supported_audio_by_content(data, expected):
    assert detect_audio_format(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"OggS",  # too short to be real
        b"%PDF-1.7" + b"\x00" * 40,
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,
        b"#!AMR\n" + b"\x00" * 40,
        b"<script>" + b"\x00" * 40,
    ],
)
def test_rejects_unknown_or_unsupported_content(data):
    assert detect_audio_format(data) is None
