"""Photos -> calendar: preparing images in memory (Pillow only, no network, no files)."""

from __future__ import annotations

import base64
import io
import sys

import pytest
from PIL import Image

from mungchi import images
from mungchi.images import (
    DEFAULT_IMAGE_PROMPT,
    HEIC_UNSUPPORTED_TEXT,
    MAX_EDGE,
    ImageError,
    load_image_files,
    prepare_image,
    select_images,
)


def encoded(image: Image.Image, fmt: str = "JPEG", **params) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **params)
    return buffer.getvalue()


def opened(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def noisy(size: tuple[int, int]) -> Image.Image:
    """Hard to compress: JPEG stays big."""
    return Image.merge("RGB", [Image.effect_noise(size, 120) for _ in range(3)])


def test_large_photos_are_shrunk_to_a_1568px_long_edge_jpeg():
    media_type, data = prepare_image(encoded(Image.new("RGB", (4000, 3000), "navy")), "image/jpeg")
    assert media_type == "image/jpeg" and data[:3] == b"\xff\xd8\xff"
    assert opened(data).size == (1568, 1176)
    _, tall = prepare_image(encoded(Image.new("RGB", (1000, 5000), "white"), "PNG"), "image/png")
    assert opened(tall).size == (314, 1568) or opened(tall).size == (313, 1568)
    _, small = prepare_image(encoded(Image.new("RGB", (300, 200), "white"), "WEBP"), "image/webp")
    assert opened(small).size == (300, 200)  # never enlarged
    assert max(opened(data).size) <= MAX_EDGE == 1568


def test_exif_rotation_is_applied_and_the_metadata_dropped():
    exif = Image.Exif()
    exif[0x0112] = 6  # "rotate 90° clockwise to display"
    exif[0x010F] = "Phone maker"
    sideways = Image.new("RGB", (400, 200), "red")
    sideways.paste(Image.new("RGB", (40, 200), "blue"), (0, 0))  # a blue band on the left
    _, data = prepare_image(encoded(sideways, exif=exif.tobytes()), "image/jpeg")
    upright = opened(data)
    assert upright.size == (200, 400)
    r, g, b = upright.getpixel((100, 10))  # the band is now on top
    assert b > 150 and r < 100
    assert not upright.getexif()  # no orientation, maker or GPS left in what is sent


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P"])
def test_transparency_becomes_white_rgb(mode):
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    image.paste(Image.new("RGBA", (16, 16), (255, 0, 0, 255)), (0, 0))
    if mode == "LA":
        image = image.convert("LA")
        data = encoded(image, "PNG")
    elif mode == "P":
        data = encoded(image.convert("P", palette=Image.Palette.ADAPTIVE), "GIF", transparency=0)
    else:
        data = encoded(image, "PNG")
    _, out = prepare_image(data, "image/png" if mode != "P" else "image/gif")
    result = opened(out)
    assert result.mode == "RGB"
    assert all(channel > 240 for channel in result.getpixel((50, 50)))  # transparent -> white


def test_animated_gifs_use_the_first_frame():
    frames = [Image.new("RGB", (50, 50), color) for color in ("green", "red")]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:])
    _, out = prepare_image(buffer.getvalue(), "image/gif")
    r, g, b = opened(out).getpixel((25, 25))
    assert g > r


def test_the_base64_size_cap_lowers_quality_then_size():
    photo = encoded(noisy((1200, 900)), quality=95)
    _, first = prepare_image(photo, "image/jpeg")
    full = images.base64_size(len(first))
    # Just below the quality-85 size: a lower quality, same size.
    _, lower = prepare_image(photo, "image/jpeg", max_base64_bytes=full - 1)
    assert images.base64_size(len(lower)) <= full - 1 and opened(lower).size == (1200, 900)
    # Far below: the image itself gets smaller until it fits.
    _, smaller = prepare_image(photo, "image/jpeg", max_base64_bytes=60_000)
    assert images.base64_size(len(smaller)) <= 60_000 and opened(smaller).size[0] < 1200
    assert len(base64.b64encode(smaller)) == images.base64_size(len(smaller))
    with pytest.raises(ImageError, match="줄여도 보낼 수 없어요"):
        prepare_image(photo, "image/jpeg", max_base64_bytes=200)
    assert images.MAX_BASE64_BYTES == 4_500_000


def test_heic_without_pillow_heif_asks_for_a_jpg(monkeypatch):
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64
    monkeypatch.setattr(images, "_heif_registered", False)
    for mimetype in ("image/heic", "image/heif", ""):  # also by its header alone
        with pytest.raises(ImageError) as caught:
            prepare_image(heic, mimetype)
        assert str(caught.value) == HEIC_UNSUPPORTED_TEXT
    assert "JPG나 PNG로 바꾸거나 화면을 캡처해서" in HEIC_UNSUPPORTED_TEXT


def test_heic_with_pillow_heif_is_read():
    pillow_heif = pytest.importorskip("pillow_heif")
    buffer = io.BytesIO()
    pillow_heif.from_pillow(Image.new("RGB", (320, 240), "orange")).save(buffer, format="HEIF", quality=60)
    assert images.looks_like_heif(buffer.getvalue()) and images.heif_available()
    media_type, data = prepare_image(buffer.getvalue(), "image/heic")
    assert media_type == "image/jpeg" and opened(data).size == (320, 240)


def test_unreadable_too_big_and_no_pillow(monkeypatch):
    with pytest.raises(ImageError, match="사진을 읽지 못했어요"):
        prepare_image(b"not an image at all", "image/png")
    with pytest.raises(ImageError, match="20MB보다 큰 사진은 읽지 않았어요"):
        prepare_image(b"\x00" * (images.MAX_FILE_BYTES + 1), "image/jpeg")
    monkeypatch.setitem(sys.modules, "PIL", None)  # Pillow not installed (e.g. git pull without pip install)
    with pytest.raises(ImageError) as caught:
        prepare_image(b"\xff\xd8\xff", "image/jpeg")
    assert "pip install -e ." in str(caught.value) and "service restart" in str(caught.value)


def test_nothing_is_written_to_disk(tmp_path):
    before = set(tmp_path.iterdir())  # the tests run in tmp_path
    prepare_image(encoded(noisy((2000, 1500))), "image/jpeg", max_base64_bytes=100_000)
    assert set(tmp_path.iterdir()) == before


def _file(name, mimetype=None, size=1000, **extra):
    file = {"id": f"F{name}", "name": name, "size": size, "url_private_download": f"https://files.slack.com/files-pri/T1-F1/{name}"}
    if mimetype is not None:
        file["mimetype"] = mimetype
    return {**file, **extra}


def test_selecting_the_images_of_a_message():
    files = [
        _file("poster.jpg", "image/jpeg"),
        _file("notice.pdf", "application/pdf"),
        _file("huge.png", "image/png", size=images.MAX_FILE_BYTES + 1),
        _file("iphone.HEIC", None),  # no mimetype: from the name
        "not a file",
    ]
    selection = select_images(files)
    assert [f["name"] for f in selection.images] == ["poster.jpg", "iphone.HEIC"] and selection.any_image
    assert selection.notes == [
        "20MB보다 큰 사진은 읽지 않았어요: huge.png",
        "지금은 사진(JPG·PNG·GIF·WebP·HEIC)만 읽을 수 있어요. PDF 같은 파일은 화면을 캡처해서 보내 주세요 (읽지 않은 파일: notice.pdf).",
    ]
    many = select_images([_file(f"{i}.png", "image/png") for i in range(7)])
    assert [f["name"] for f in many.images] == ["0.png", "1.png", "2.png", "3.png", "4.png"]
    assert many.notes == ["사진은 한 번에 5장까지만 읽어요. 앞의 5장만 볼게요."]
    only_pdf = select_images([_file("a.pdf", "application/pdf")])
    assert only_pdf.images == [] and not only_pdf.any_image and len(only_pdf.notes) == 1
    assert images.ACCEPTED_MIMETYPES == ("image/jpeg", "image/png", "image/gif", "image/webp", "image/heic", "image/heif")


def test_command_line_images_are_checked_and_prepared(tmp_path):
    good = tmp_path / "poster.png"
    good.write_bytes(encoded(Image.new("RGB", (2000, 1000), "white"), "PNG"))
    [(media_type, data)] = load_image_files([good])
    assert media_type == "image/jpeg" and opened(data).size == (1568, 784)
    with pytest.raises(ImageError, match="사진 파일을 찾을 수 없어요"):
        load_image_files([tmp_path / "missing.jpg"])
    pdf = tmp_path / "notice.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    with pytest.raises(ImageError, match=r"사진\(JPG·PNG·GIF·WebP·HEIC\)만 보낼 수 있어요: notice.pdf"):
        load_image_files([pdf])
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"\xff\xd8 nope")
    with pytest.raises(ImageError, match="broken.jpg: 사진을 읽지 못했어요"):
        load_image_files([broken])
    with pytest.raises(ImageError, match="5장까지만"):
        load_image_files([good] * 6)
    assert DEFAULT_IMAGE_PROMPT == "이 이미지에 있는 일정을 캘린더에 등록해줘"
