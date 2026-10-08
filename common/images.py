"""Receipt image validation and normalization (no business rules).

Untrusted bytes in → re-encoded bytes out:

- format detected from magic bytes (JPEG, PNG, WEBP, static GIF) and confirmed by Pillow;
  declared MIME, extension and file name are never used;
- size limit on input and output; pixel limit enforced *before* decoding (Pillow's
  decompression-bomb check turned into an error);
- EXIF orientation applied, then the image is re-encoded **without any metadata** (EXIF/GPS,
  ICC text chunks, comments). JPEG stays JPEG (quality 92, no extra subsampling), PNG stays
  PNG, WEBP stays WEBP (quality 92), static GIF becomes PNG;
- SHA-256 of the normalized bytes sent to the vision model; no image is retained.

No OCR here: reading the document is the vision model's job.
"""

import hashlib
import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

MIN_SIDE = 32
MAX_SIDE = 12_000


class InvalidImage(ValueError):
    """Not a supported, sane image. ``code`` is safe to store/log."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class NormalizedImage:
    data: bytes
    extension: str
    content_type: str
    width: int
    height: int
    sha256: str

    def __repr__(self) -> str:  # never dump image bytes
        return (
            f"NormalizedImage({self.content_type}, {self.width}x{self.height}, "
            f"bytes={len(self.data)})"
        )


_FORMATS = {
    # sniffed name: (Pillow format, output Pillow format, extension, content type)
    "jpeg": ("JPEG", "JPEG", "jpg", "image/jpeg"),
    "png": ("PNG", "PNG", "png", "image/png"),
    "webp": ("WEBP", "WEBP", "webp", "image/webp"),
    "gif": ("GIF", "PNG", "png", "image/png"),
}


def sniff_image_format(data: bytes) -> str | None:
    if len(data) < 12:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    return None


def normalize_image(data: bytes, *, max_bytes: int, max_pixels: int) -> NormalizedImage:
    if not data:
        raise InvalidImage("IMAGE_EMPTY")
    if len(data) > max_bytes:
        raise InvalidImage("IMAGE_TOO_LARGE")
    sniffed = sniff_image_format(data)
    if sniffed is None:
        raise InvalidImage("IMAGE_UNSUPPORTED_FORMAT")
    pil_format, out_format, extension, content_type = _FORMATS[sniffed]

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=[pil_format]) as probe:
                _check_dimensions(probe.size, max_pixels)
                if getattr(probe, "n_frames", 1) > 1:
                    raise InvalidImage("IMAGE_ANIMATED")
                probe.verify()  # structural check; the object is unusable afterwards
            with Image.open(io.BytesIO(data), formats=[pil_format]) as image:
                image.load()
                image = ImageOps.exif_transpose(image)
                _check_dimensions(image.size, max_pixels)
                output = _encode(image, out_format)
    except InvalidImage:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise InvalidImage("IMAGE_TOO_MANY_PIXELS") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError):
        raise InvalidImage("IMAGE_CORRUPTED") from None

    if len(output) > max_bytes:
        raise InvalidImage("IMAGE_TOO_LARGE")
    width, height = _size_of(output)
    return NormalizedImage(
        data=output,
        extension=extension,
        content_type=content_type,
        width=width,
        height=height,
        sha256=hashlib.sha256(output).hexdigest(),
    )


def _check_dimensions(size: tuple[int, int], max_pixels: int) -> None:
    width, height = size
    if width < MIN_SIDE or height < MIN_SIDE:
        raise InvalidImage("IMAGE_TOO_SMALL")
    if width > MAX_SIDE or height > MAX_SIDE or width * height > max_pixels:
        raise InvalidImage("IMAGE_TOO_MANY_PIXELS")


def _encode(image: Image.Image, out_format: str) -> bytes:
    # PNG implicitly inherits ICC from image.info, even without a save argument.
    # Keep transparency until conversion so stripping metadata preserves visible pixels.
    image.info.pop("icc_profile", None)
    buffer = io.BytesIO()
    if out_format == "JPEG":
        image.convert("RGB").save(buffer, "JPEG", quality=92, subsampling=0, optimize=True)
    elif out_format == "WEBP":
        mode = "RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB"
        image.convert(mode).save(buffer, "WEBP", quality=92, method=4)
    else:  # PNG (also for static GIF)
        mode = "RGBA" if image.mode in ("RGBA", "LA", "P", "PA") else "RGB"
        image.convert(mode).save(buffer, "PNG", optimize=True)
    # EXIF/text are not supplied; ICC cannot be inherited. Transparency is kept as pixels.
    return buffer.getvalue()


def _size_of(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.size
