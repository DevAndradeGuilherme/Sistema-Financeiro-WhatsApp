"""Media helpers (no business rules). Formats are detected from content, never trusted from
declared MIME types, extensions or filenames supplied by external parties."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AudioFormat:
    extension: str
    content_type: str


OGG = AudioFormat("ogg", "audio/ogg")
MP3 = AudioFormat("mp3", "audio/mpeg")
MP4 = AudioFormat("m4a", "audio/mp4")
WAV = AudioFormat("wav", "audio/wav")
WEBM = AudioFormat("webm", "audio/webm")


def detect_audio_format(data: bytes) -> AudioFormat | None:
    """Audio container accepted by the transcription API, by magic bytes; None otherwise.

    WhatsApp voice notes are OGG/Opus, sent as-is (no conversion).
    """
    if len(data) < 12:
        return None
    if data.startswith(b"OggS"):
        return OGG
    if data[4:8] == b"ftyp":
        return MP4
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return WAV
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return WEBM
    if data.startswith(b"ID3") or (data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return MP3
    return None
