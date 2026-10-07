import io
import struct
import zlib

import pytest
from PIL import Image

from common.images import InvalidImage, normalize_image, sniff_image_format

MAX_BYTES = 1024 * 1024
MAX_PIXELS = 4_000_000


def make(fmt: str, size=(120, 80), **save_kwargs) -> bytes:
    image = Image.new("RGB", size, (200, 30, 30))
    buffer = io.BytesIO()
    image.save(buffer, fmt, **save_kwargs)
    return buffer.getvalue()


def normalize(data, **kwargs):
    return normalize_image(
        data,
        max_bytes=kwargs.get("max_bytes", MAX_BYTES),
        max_pixels=kwargs.get("max_pixels", MAX_PIXELS),
    )


@pytest.mark.parametrize(
    "fmt, ext, content_type",
    [
        ("JPEG", "jpg", "image/jpeg"),
        ("PNG", "png", "image/png"),
        ("WEBP", "webp", "image/webp"),
        ("GIF", "png", "image/png"),  # static GIF re-encoded as PNG
    ],
)
def test_supported_formats_are_normalized(fmt, ext, content_type):
    result = normalize(make(fmt))
    assert (result.extension, result.content_type) == (ext, content_type)
    assert (result.width, result.height) == (120, 80)
    assert len(result.sha256) == 64
    assert sniff_image_format(result.data) is not None
    assert "bytes=" in repr(result) and str(result.data[:4]) not in repr(result)


def test_hash_is_deterministic():
    data = make("PNG")
    assert normalize(data).sha256 == normalize(data).sha256


@pytest.mark.parametrize(
    "data, code",
    [
        (b"", "IMAGE_EMPTY"),
        (b"this is not an image but named receipt.jpg" * 3, "IMAGE_UNSUPPORTED_FORMAT"),
        (b"%PDF-1.7\n" + b"0" * 100, "IMAGE_UNSUPPORTED_FORMAT"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 100, "IMAGE_CORRUPTED"),  # JPEG magic, garbage body
    ],
)
def test_invalid_inputs(data, code):
    with pytest.raises(InvalidImage) as exc:
        normalize(data)
    assert exc.value.code == code


def test_truncated_image_is_corrupted():
    data = make("PNG", size=(300, 300))
    with pytest.raises(InvalidImage) as exc:
        normalize(data[: len(data) // 2])
    assert exc.value.code == "IMAGE_CORRUPTED"


def test_size_limits():
    data = make("PNG")
    assert normalize(data, max_bytes=len(data) * 4).width == 120  # within limit
    with pytest.raises(InvalidImage) as exc:
        normalize(data, max_bytes=len(data) - 1)
    assert exc.value.code == "IMAGE_TOO_LARGE"


def test_input_exactly_at_limit_is_accepted():
    data = make("PNG")
    assert normalize(data, max_bytes=max(len(data), len(normalize(data).data))).sha256


def _png_with_dimensions(width: int, height: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00" * 10))
        + chunk(b"IEND", b"")
    )


@pytest.mark.parametrize("size", [(50_000, 50_000), (11_000, 10), (3000, 3000)])
def test_absurd_dimensions_rejected_before_decoding(size):
    """A tiny file declaring a huge canvas (decompression bomb) never gets decoded."""
    with pytest.raises(InvalidImage) as exc:
        normalize(_png_with_dimensions(*size))
    assert exc.value.code in ("IMAGE_TOO_MANY_PIXELS", "IMAGE_TOO_SMALL")


def test_pixel_limit_is_configurable():
    with pytest.raises(InvalidImage) as exc:
        normalize(make("PNG", size=(100, 100)), max_pixels=5_000)
    assert exc.value.code == "IMAGE_TOO_MANY_PIXELS"


def test_too_small_rejected():
    with pytest.raises(InvalidImage) as exc:
        normalize(make("PNG", size=(10, 10)))
    assert exc.value.code == "IMAGE_TOO_SMALL"


def test_animated_gif_rejected():
    frames = [Image.new("RGB", (64, 64), color) for color in ((255, 0, 0), (0, 255, 0))]
    buffer = io.BytesIO()
    frames[0].save(buffer, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    with pytest.raises(InvalidImage) as exc:
        normalize(buffer.getvalue())
    assert exc.value.code == "IMAGE_ANIMATED"


def test_exif_and_gps_removed_and_orientation_applied():
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° CW
    exif[0x010F] = "SecretCam"  # Make
    exif[0x8825] = {1: "S", 2: (23.0, 33.0, 0.0), 3: "W", 4: (46.0, 38.0, 0.0)}  # GPSInfo
    original = make("JPEG", size=(120, 80), exif=exif.tobytes())
    with Image.open(io.BytesIO(original)) as check:
        assert check.getexif().get(0x0112) == 6  # the input really has EXIF/GPS

    result = normalize(original)
    with Image.open(io.BytesIO(result.data)) as cleaned:
        assert dict(cleaned.getexif()) == {}
        assert "exif" not in cleaned.info
    assert (result.width, result.height) == (80, 120)  # orientation applied, then dropped
    assert b"SecretCam" not in result.data and b"GPS" not in result.data


def test_png_text_metadata_removed():
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    info.add_text("Author", "Fulano de Tal CPF 000.000.000-00")
    data = make("PNG", pnginfo=info)
    assert b"Fulano" in data
    assert b"Fulano" not in normalize(data).data
