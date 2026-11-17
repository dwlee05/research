"""Photos -> calendar: pick the images of a Slack message and shrink them in memory.

A poster, an email screenshot or a timetable sent to 업뎃 or 일정 (Slack or
``--image``) goes to the agent as image content blocks, next to the text.
Before that every image is checked and re-encoded here, entirely in memory
(nothing is written to disk and image bytes are never logged):

* ``ImageOps.exif_transpose`` (photos taken sideways come out upright),
* RGB (transparency on white),
* the long edge at most ``MAX_EDGE`` (1568 px, about 2.5k input tokens at
  1568×1176),
* JPEG at quality 85, lower until the base64 payload is at most
  ``MAX_BASE64_BYTES`` (about 4.5 MB). Re-encoding drops EXIF metadata such
  as the GPS position.

HEIC/HEIF (iPhone photos) needs ``pillow-heif``. Without it a HEIC photo gets
a Korean note asking for a JPG/PNG or a screenshot. Pillow itself is imported
only when an image is prepared, so a bot updated with ``git pull`` but not yet
``pip install -e .`` still starts (and says what to install when a photo comes).
"""

from __future__ import annotations

import base64
import io
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

if TYPE_CHECKING:
    from PIL import Image

ACCEPTED_MIMETYPES = ("image/jpeg", "image/png", "image/gif", "image/webp", "image/heic", "image/heif")
HEIF_MIMETYPES = frozenset({"image/heic", "image/heif"})
MAX_IMAGES = 5
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_EDGE = 1568
JPEG_QUALITY = 85
MIN_JPEG_QUALITY = 45
QUALITY_STEP = 10
# The API accepts about 5 MB per image; stay below it with room to spare.
MAX_BASE64_BYTES = 4_500_000
# Below this edge an image is not shrunk any further to fit the size cap.
MIN_EDGE = 256
OUTPUT_MEDIA_TYPE = "image/jpeg"
# What the agent is asked when a photo comes without any text.
DEFAULT_IMAGE_PROMPT = "이 이미지에 있는 일정을 캘린더에 등록해줘"

FORMATS_TEXT = "JPG·PNG·GIF·WebP·HEIC"
HEIC_UNSUPPORTED_TEXT = (
    "HEIC 사진은 아직 읽을 수 없어요(pillow-heif가 설치되어 있지 않아요). "
    "JPG나 PNG로 바꾸거나 화면을 캡처해서 보내 주세요."
)
UNREADABLE_TEXT = "사진을 읽지 못했어요. JPG나 PNG로 다시 보내 주세요."
PILLOW_MISSING_TEXT = (
    "사진을 읽는 데 필요한 Pillow가 설치되어 있지 않아요. 저장소 폴더에서 가상환경을 켜고 "
    "pip install -e . 를 다시 실행한 뒤 봇을 다시 시작하세요(python -m mungchi service restart)."
)
TOO_BIG_TEXT = f"{MAX_FILE_BYTES // (1024 * 1024)}MB보다 큰 사진은 읽지 않았어요"
TOO_MANY_TEXT = f"사진은 한 번에 {MAX_IMAGES}장까지만 읽어요. 앞의 {MAX_IMAGES}장만 볼게요."
UNSUPPORTED_FILES_TEXT = (
    f"지금은 사진({FORMATS_TEXT})만 읽을 수 있어요. PDF 같은 파일은 화면을 캡처해서 보내 주세요"
)
MAX_NAME_CHARS = 40

# (media type, bytes) of one prepared image, as ``run_turn`` takes it.
ImageInput = tuple[str, bytes]


class ImageError(ValueError):
    """An image that cannot be sent; ``str()`` is a Korean line for the user."""


# ---------------------------------------------------------------- Pillow and HEIC support


def load_pillow() -> tuple[Any, Any, Any]:
    """``(Image, ImageOps, UnidentifiedImageError)``; ``ImageError`` (what to install) without Pillow."""
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError:
        raise ImageError(PILLOW_MISSING_TEXT) from None
    return Image, ImageOps, UnidentifiedImageError


_heif_registered: bool | None = None


def heif_available() -> bool:
    """True when ``pillow-heif`` is installed (its opener is registered with Pillow once)."""
    global _heif_registered
    if _heif_registered is None:
        try:
            from pillow_heif import register_heif_opener  # type: ignore[import-not-found]
        except ImportError:
            _heif_registered = False
        else:
            register_heif_opener()
            _heif_registered = True
    return _heif_registered


_HEIF_BRANDS = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1"})


def looks_like_heif(data: bytes) -> bool:
    """HEIF/HEIC by its file header (``....ftypheic``), whatever the file is called."""
    return len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS


# ---------------------------------------------------------------- preparing one image


def base64_size(size: int) -> int:
    """Length of ``size`` bytes once base64-encoded."""
    return 4 * ((size + 2) // 3)


def _to_rgb(image: Image.Image) -> Image.Image:
    pil_image, _ops, _unidentified = load_pillow()
    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = pil_image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image if image.mode == "RGB" else image.convert("RGB")


def _jpeg(image: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def encode_within_limit(image: Image.Image, max_base64_bytes: int = MAX_BASE64_BYTES) -> bytes:
    """JPEG bytes of ``image`` whose base64 form fits ``max_base64_bytes``.

    Quality 85 first, then 10 lower at a time down to 45, then a smaller
    image (three quarters of the size each time) until it fits.
    """
    pil_image, _ops, _unidentified = load_pillow()
    quality = JPEG_QUALITY
    while True:
        data = _jpeg(image, quality)
        if base64_size(len(data)) <= max_base64_bytes:
            return data
        if quality - QUALITY_STEP >= MIN_JPEG_QUALITY:
            quality -= QUALITY_STEP
            continue
        width, height = image.size
        if max(width, height) <= MIN_EDGE:
            raise ImageError("사진이 너무 커서 줄여도 보낼 수 없어요. 일정 부분만 캡처해서 보내 주세요.")
        image = image.resize((max(1, width * 3 // 4), max(1, height * 3 // 4)), pil_image.Resampling.LANCZOS)


def prepare_image(data: bytes, mimetype: str = "", *, max_base64_bytes: int = MAX_BASE64_BYTES) -> ImageInput:
    """``(media type, bytes)`` ready for the agent: upright, RGB, long edge ≤ 1568 px, JPEG within the size cap.

    Raises ``ImageError`` (a Korean line) for an image that cannot be read,
    a HEIC photo without ``pillow-heif``, or a file over 20 MB. In memory only.
    """
    if len(data) > MAX_FILE_BYTES:
        raise ImageError(TOO_BIG_TEXT + ".")
    pil_image, image_ops, unidentified = load_pillow()
    heif = (mimetype or "").lower() in HEIF_MIMETYPES or looks_like_heif(data)
    if heif and not heif_available():
        raise ImageError(HEIC_UNSUPPORTED_TEXT)
    try:
        with pil_image.open(io.BytesIO(data)) as opened:
            opened.draft("RGB", (MAX_EDGE, MAX_EDGE))  # JPEG: decode at a smaller size when it can
            image = image_ops.exif_transpose(opened)  # a loaded copy of the first frame (GIF: the first one)
            image = _to_rgb(image)
    except (unidentified, OSError, ValueError, pil_image.DecompressionBombError, SyntaxError):
        raise ImageError(UNREADABLE_TEXT) from None
    image.thumbnail((MAX_EDGE, MAX_EDGE), pil_image.Resampling.LANCZOS)
    return OUTPUT_MEDIA_TYPE, encode_within_limit(image, max_base64_bytes)


def to_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------- choosing the images of a Slack message


def short_name(name: Any) -> str:
    text = " ".join(str(name or "").split()) or "(이름 없음)"
    return text if len(text) <= MAX_NAME_CHARS else text[: MAX_NAME_CHARS - 1] + "…"


def file_mimetype(file: Mapping[str, Any]) -> str:
    """A Slack file's mimetype; from its name when Slack left it out (``.heic`` -> ``image/heic``)."""
    mimetype = str(file.get("mimetype") or "").lower().strip()
    if mimetype:
        return mimetype
    guessed = guess_mimetype(str(file.get("name") or ""))
    return guessed or ""


def guess_mimetype(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in (".heic", ".heif"):
        return f"image/{suffix[1:]}"
    guessed, _encoding = mimetypes.guess_type(name)
    return (guessed or "").lower()


@dataclass
class FileSelection:
    """The images of one message to read, and Korean notes about the files left out."""

    images: list[Mapping[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Some file was an image (read or not): 고뭉치 redirects these to 업뎃 / 일정.
    any_image: bool = False


def select_images(files: Iterable[Any]) -> FileSelection:
    """Which files of a Slack message to download: images only, at most 5, each at most 20 MB. Pure.

    Sizes come from Slack's file objects; the download checks them again.
    """
    selection = FileSelection()
    too_big: list[str] = []
    other: list[str] = []
    for file in files:
        if not isinstance(file, Mapping):
            continue
        mimetype = file_mimetype(file)
        if mimetype not in ACCEPTED_MIMETYPES:
            other.append(short_name(file.get("name") or file.get("title")))
            continue
        selection.any_image = True
        try:
            size = int(file.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > MAX_FILE_BYTES:
            too_big.append(short_name(file.get("name")))
            continue
        selection.images.append(file)
    if len(selection.images) > MAX_IMAGES:
        selection.images = selection.images[:MAX_IMAGES]
        selection.notes.append(TOO_MANY_TEXT)
    if too_big:
        selection.notes.append(f"{TOO_BIG_TEXT}: {', '.join(too_big)}")
    if other:
        selection.notes.append(f"{UNSUPPORTED_FILES_TEXT} (읽지 않은 파일: {', '.join(other)}).")
    return selection


# ---------------------------------------------------------------- files given on the command line (--image)


def load_image_files(paths: Iterable[str | Path]) -> list[ImageInput]:
    """Read and prepare the ``--image`` files (at most 5) the same way as Slack photos.

    Raises ``ImageError`` naming the file for a missing, too big, unsupported
    or unreadable one. Nothing is written anywhere.
    """
    paths = list(paths)
    if len(paths) > MAX_IMAGES:
        raise ImageError(f"사진은 한 번에 {MAX_IMAGES}장까지만 보낼 수 있어요(받은 사진 {len(paths)}장).")
    prepared: list[ImageInput] = []
    for raw in paths:
        path = Path(raw).expanduser()
        name = short_name(path.name)
        if not path.is_file():
            raise ImageError(f"사진 파일을 찾을 수 없어요: {raw}")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ImageError(f"{TOO_BIG_TEXT}: {name}")
        data = path.read_bytes()
        mimetype = guess_mimetype(path.name)
        if mimetype not in ACCEPTED_MIMETYPES and not looks_like_heif(data):
            raise ImageError(f"사진({FORMATS_TEXT})만 보낼 수 있어요: {name}")
        try:
            prepared.append(prepare_image(data, mimetype))
        except ImageError as exc:
            raise ImageError(f"{name}: {exc}") from None
    return prepared
