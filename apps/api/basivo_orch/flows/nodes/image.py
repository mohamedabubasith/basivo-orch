"""Making photographs usable before they become a video.

What arrives from a studio is not what a montage needs. Phone pictures are
rotated by an EXIF flag rather than by their pixels, a mirrorless camera sends
45 megapixels when 2 will do, and a set of photographs is a mix of portrait and
landscape that a single frame has to reconcile. Left alone, the montage shows
sideways pictures, runs out of memory, and crops the groom's head off.

Every operation here is deliberate about one thing: **it never invents detail.**
It orients, it shrinks, it chooses which part of a frame to keep, it corrects
exposure. What it does not do is upscale, beautify faces, or otherwise return a
photograph the studio did not take — because the studio's name goes on this.
"""

from __future__ import annotations

import io
from typing import Any, Literal

from pydantic import BaseModel, Field

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.templating import render_value

Operation = Literal["auto_orient", "fit", "smart_crop", "enhance", "remove_background"]

#: Decoding is where a hostile image does its damage: a 400MB PNG that expands
#: to 40 gigapixels costs the worker its memory long before anything renders.
#: Pillow's own bomb check is a warning by default; this is the hard stop.
MAX_INPUT_PIXELS = 80_000_000
MAX_INPUT_BYTES = 25 * 1024 * 1024
#: Past this there is nothing to gain: a 1080p frame is 2 megapixels, and every
#: pixel beyond that is render time spent on detail the video cannot show.
DEFAULT_LONG_EDGE = 2200


class ImageEditConfig(BaseModel):
    model_config = {"extra": "forbid"}

    artifact_id: str = Field(default="", title="Photo")
    operations: list[Operation] = Field(
        default_factory=lambda: ["auto_orient", "fit", "enhance"],
        title="What to do",
    )
    #: For `smart_crop`. "9:16" for a phone video, "16:9" for a screen.
    aspect: str = Field(default="9:16", title="Shape")
    long_edge: int = Field(default=DEFAULT_LONG_EDGE, ge=200, le=6000, title="Longest side")
    #: How hard `enhance` pushes. Gentle by default: a wedding photograph has
    #: already been graded by the studio, and an automatic pass that fights
    #: their grade is worse than none.
    strength: float = Field(default=0.35, ge=0.0, le=1.0, title="Correction strength")
    output_format: Literal["jpeg", "png", "webp"] = Field(default="jpeg", title="Save as")
    quality: int = Field(default=88, ge=40, le=100, title="Quality")


class ImageEditNode(Node):
    type = "image.edit"
    label = "Prepare Photo"
    description = "Orient, resize, crop and correct a photo before it is used."
    tier = 1
    category = "design"
    config_model = ImageEditConfig
    output_paths = ("artifact_id", "url", "width", "height", "size_bytes", "applied")

    async def run(self, config: ImageEditConfig, ctx: NodeContext) -> NodeResult:
        if ctx.load_artifact is None or ctx.save_artifact is None:
            raise NodeError("Preparing a photo is only possible inside a real run.")

        template = ctx.template_context()
        artifact_id = str(render_value(config.artifact_id, template)).strip()
        if not artifact_id:
            raise NodeError(
                "No photo to prepare. This is usually {{ input.photos.0.artifact_id }} "
                "from the Telegram trigger, or a photo from the conversation."
            )

        data = await ctx.load_artifact(artifact_id)
        if data is None:
            raise NodeError("That photo is no longer stored, so there is nothing to prepare.")
        if len(data) > MAX_INPUT_BYTES:
            raise NodeError(
                f"That file is {len(data) // (1024 * 1024)}MB and the limit is "
                f"{MAX_INPUT_BYTES // (1024 * 1024)}MB."
            )

        image, applied = _process(data, config)

        buffer = io.BytesIO()
        save_format = config.output_format.upper()
        if save_format == "JPEG" and image.mode not in {"RGB", "L"}:
            # A JPEG has no alpha, and Pillow's error for this is unhelpful.
            image = image.convert("RGB")
        # No `exif=` argument on purpose: the original carries GPS coordinates
        # and a camera serial, and a wedding video's frames should not.
        image.save(buffer, format=save_format, quality=config.quality, optimize=True)
        prepared = buffer.getvalue()

        saved = await ctx.save_artifact(
            prepared,
            filename=f"prepared.{config.output_format}",
            content_type=f"image/{config.output_format}",
            node_id=ctx.node_id,
        )
        await ctx.step(
            "image.prepared",
            {
                "applied": applied,
                "width": image.width,
                "height": image.height,
                "bytes": len(prepared),
            },
        )
        return NodeResult(
            output={
                **saved,
                "width": image.width,
                "height": image.height,
                "applied": applied,
            }
        )


def _process(data: bytes, config: ImageEditConfig) -> tuple[Any, list[str]]:
    """Everything that touches pixels, in one place and without I/O.

    Separate from the node so the interesting decisions can be tested against
    real images without a database, a run, or a mocked context.
    """
    from PIL import Image, ImageEnhance, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_INPUT_PIXELS

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:  # noqa: BLE001 - Pillow raises many things
        raise NodeError(
            f"That file is not an image this can read ({type(exc).__name__}). "
            "Telegram will happily forward a PDF or a HEIC the sender's phone made."
        ) from None

    applied: list[str] = []

    for operation in config.operations:
        if operation == "auto_orient":
            # The single most valuable operation here. A phone writes the
            # picture in sensor order and an EXIF tag saying which way is up;
            # anything that ignores the tag shows it on its side.
            image = ImageOps.exif_transpose(image)
            applied.append("auto_orient")

        elif operation == "fit":
            if max(image.size) > config.long_edge:
                image.thumbnail((config.long_edge, config.long_edge), Image.LANCZOS)
                applied.append("fit")

        elif operation == "smart_crop":
            image = _smart_crop(image, _parse_aspect(config.aspect))
            applied.append("smart_crop")

        elif operation == "enhance":
            # Autocontrast with a cut, not a full stretch: a photograph with a
            # legitimately dark background should keep it.
            image = image.convert("RGB")
            image = ImageOps.autocontrast(image, cutoff=(1, 1))
            if config.strength:
                image = ImageEnhance.Color(image).enhance(1 + 0.25 * config.strength)
                image = ImageEnhance.Sharpness(image).enhance(1 + 0.6 * config.strength)
            applied.append("enhance")

        elif operation == "remove_background":
            image = _remove_background(image)
            applied.append("remove_background")

    return image, applied


def _parse_aspect(aspect: str) -> float:
    try:
        width, height = (float(part) for part in aspect.replace("x", ":").split(":", 1))
        if width <= 0 or height <= 0:
            raise ValueError
        return width / height
    except ValueError:
        raise NodeError(f"{aspect!r} is not a shape. Write it like 9:16 or 16:9.") from None


def _smart_crop(image: Any, target_ratio: float):
    """Crop to a shape by keeping the busiest part of the frame.

    Not face detection, and honest about it: this finds detail, and in a
    photograph of people the detail is usually the people. Two deliberate
    biases make it behave on portraits — the window is scored on edge energy,
    and ties are broken towards the top, because when a standing figure does not
    fit in a 9:16 frame the half worth keeping is the half with the face in it.

    A studio that disagrees can re-send the picture already cropped, which is
    why this never crops more than it must.
    """
    from PIL import Image, ImageFilter

    width, height = image.size
    current = width / height
    if abs(current - target_ratio) < 0.01:
        return image

    if current > target_ratio:
        # Too wide: slide a full-height window horizontally.
        window = int(round(height * target_ratio))
        axis, span, limit = "x", window, width
    else:
        # Too tall: slide a full-width window vertically.
        window = int(round(width / target_ratio))
        axis, span, limit = "y", window, height

    # Edge energy on a small greyscale copy. Cheap, and enough: this decides
    # between a few dozen candidate windows, not the pixels themselves.
    probe = image.convert("L").resize((160, 160), Image.BILINEAR).filter(ImageFilter.FIND_EDGES)
    # `tobytes()` rather than `getdata()`: same values for an 8-bit greyscale
    # image, no deprecation, and one allocation instead of 25,600.
    values = probe.tobytes()
    energy_x = [sum(values[row * 160 + column] for row in range(160)) for column in range(160)]
    energy_y = [sum(values[row * 160 + column] for column in range(160)) for row in range(160)]
    energy = energy_x if axis == "x" else energy_y

    steps = 24
    best_offset, best_score = 0, -1.0
    for step in range(steps + 1):
        offset = int(round((limit - span) * step / steps))
        start = int(offset / limit * 160)
        end = max(start + 1, int((offset + span) / limit * 160))
        score = float(sum(energy[start:end]))
        # The tie-break: earlier windows win, and on the vertical axis earlier
        # means higher up.
        if score > best_score * 1.02:
            best_offset, best_score = offset, score

    if axis == "x":
        return image.crop((best_offset, 0, best_offset + span, height))
    return image.crop((0, best_offset, width, best_offset + span))


def _remove_background(image: Any):
    """Cut the subject out, when the deployment has the model for it.

    Kept optional because it is a 176MB model and half a gigabyte of peak
    memory for something a montage needs on one photograph in twenty — the
    title card. A worker without it says so plainly rather than failing with an
    ImportError nobody can act on.
    """
    try:
        from rembg import remove
    except ImportError:
        raise NodeError(
            "Background removal is not enabled on this deployment. It needs the "
            "`rembg` model in the worker image, which adds about 176MB. Everything "
            "else on this node works without it."
        ) from None

    return remove(image.convert("RGBA"))
