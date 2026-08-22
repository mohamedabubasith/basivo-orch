"""Preparing photographs, against images that actually cause trouble.

Every case here is one a studio will hit in the first week: a phone picture
that is only upright because of an EXIF tag, a 45 megapixel camera file, a
portrait that has to fit a landscape frame, and a PDF someone sent by mistake.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from basivo_orch.flows.nodes.base import NodeError
from basivo_orch.flows.nodes.image import ImageEditConfig, _process, _smart_crop


def an_image(width: int, height: int, *, colour=(120, 90, 60), exif_rotate: int = 0) -> bytes:
    image = Image.new("RGB", (width, height), colour)
    # A bright block so a crop can be shown to have kept the interesting part.
    for x in range(width // 4, width // 2):
        for y in range(height // 8, height // 4):
            image.putpixel((x, y), (255, 255, 255))
    buffer = io.BytesIO()
    if exif_rotate:
        exif = image.getexif()
        exif[274] = exif_rotate  # Orientation
        image.save(buffer, format="JPEG", exif=exif)
    else:
        image.save(buffer, format="JPEG")
    return buffer.getvalue()


def test_a_sideways_phone_photo_is_stood_up():
    """Orientation 6 means "rotate 90° clockwise to view". A montage that
    ignores the tag shows the whole wedding on its side."""
    data = an_image(400, 200, exif_rotate=6)
    result, applied = _process(data, ImageEditConfig(operations=["auto_orient"]))

    assert "auto_orient" in applied
    assert result.size == (200, 400), "the tag was applied to the pixels"


def test_a_huge_camera_file_is_brought_down():
    data = an_image(4000, 3000)
    result, applied = _process(data, ImageEditConfig(operations=["fit"], long_edge=1600))

    assert max(result.size) == 1600
    assert result.size == (1600, 1200), "the shape is kept"
    assert "fit" in applied


def test_an_image_already_small_enough_is_left_alone():
    data = an_image(800, 600)
    result, applied = _process(data, ImageEditConfig(operations=["fit"], long_edge=1600))

    assert result.size == (800, 600)
    assert applied == [], "nothing was done, and nothing is claimed"


def test_a_landscape_photo_cropped_for_a_phone_video():
    data = an_image(2000, 1000)
    result, _ = _process(data, ImageEditConfig(operations=["smart_crop"], aspect="9:16"))

    ratio = result.width / result.height
    assert abs(ratio - 9 / 16) < 0.02, f"got {ratio:.3f}"
    assert result.height == 1000, "the full height is kept; only width is given up"


def test_a_portrait_photo_cropped_for_a_screen():
    data = an_image(1000, 2000)
    result, _ = _process(data, ImageEditConfig(operations=["smart_crop"], aspect="16:9"))

    assert abs(result.width / result.height - 16 / 9) < 0.02
    assert result.width == 1000


def test_the_crop_keeps_the_busy_part_of_the_frame():
    """The whole point of a smart crop. The bright block is on the left, so a
    centre crop would cut it and this must not."""
    image = Image.new("RGB", (1200, 600), (10, 10, 10))
    for x in range(60, 300):
        for y in range(150, 450):
            image.putpixel((x, y), (255, 255, 255))

    cropped = _smart_crop(image, 1.0)  # square out of a wide frame

    assert cropped.size == (600, 600)
    # The window must include the block, which lives between x=60 and x=300.
    kept = cropped.convert("L").getextrema()
    assert kept[1] > 200, "the bright subject was cropped away"


def test_a_crop_that_is_already_the_right_shape_is_untouched():
    image = Image.new("RGB", (900, 1600), (30, 30, 30))
    assert _smart_crop(image, 9 / 16) is image


def test_enhance_does_not_wreck_a_dark_photograph():
    """A first dance is meant to be dark. An automatic pass that stretches it
    to daylight is worse than doing nothing."""
    dark = Image.new("RGB", (200, 200), (18, 16, 24))
    buffer = io.BytesIO()
    dark.save(buffer, format="JPEG")

    result, _ = _process(buffer.getvalue(), ImageEditConfig(operations=["enhance"]))
    mean = sum(result.convert("L").getdata()) / (200 * 200)

    assert mean < 90, f"the dark photograph was blown out to {mean:.0f}"


def test_something_that_is_not_an_image_says_so():
    with pytest.raises(NodeError, match="not an image"):
        _process(b"%PDF-1.7\nnot a photograph at all", ImageEditConfig())


def test_a_bad_aspect_is_explained():
    with pytest.raises(NodeError, match="9:16"):
        _process(
            an_image(100, 100), ImageEditConfig(operations=["smart_crop"], aspect="widescreen")
        )


def test_operations_run_in_the_order_given():
    """Orient before crop, always: cropping a sideways picture crops the wrong
    edge, and no later operation can undo that."""
    data = an_image(400, 200, exif_rotate=6)
    result, applied = _process(
        data, ImageEditConfig(operations=["auto_orient", "smart_crop"], aspect="9:16")
    )
    assert applied == ["auto_orient", "smart_crop"]
    assert result.width < result.height, "the result is portrait, as a 9:16 crop must be"
