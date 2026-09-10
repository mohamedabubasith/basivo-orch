"""Images, written by a model and drawn by a browser.

The obvious way to make a poster is to ask an image model for one. It is also
the wrong way: text-to-image models top out around ninety percent accuracy on
typography, which means one poster in ten has a typo in the customer's own
company name — unusable unattended, and the failure is silent.

So the model never draws the poster. It writes **HTML**, which a headless
browser renders at print scale with real fonts. The headline is the headline,
the brand colour is the brand colour, and running it twice produces the same
file. What a model is genuinely good at — layout, copy, taste in CSS — it
does. What it is bad at — drawing letterforms — it never touches.

One node, not two. Asking someone to wire an agent into a renderer meant
knowing what a Remotion-shaped prompt looks like before they got a picture;
the loop that writes, looks and fixes belongs inside the node, the same way it
does for video.

**The page is treated as hostile.** HTML that reaches here was written by a
model working from a brief that may itself have come from a stranger, so it
renders with JavaScript disabled and no access to the network beyond the fonts
explicitly allowed. Photographs are inlined as data URIs rather than fetched.
"""

from __future__ import annotations

import asyncio
import base64
import re
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, model_validator

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.nodes.video import (
    MAX_IMAGES,
    frame_spread,
    message_text_of,
    photo_ids,
    strip_code_fences,
)
from basivo_orch.flows.templating import render_value

#: Presets, so nobody has to remember that a story is 1080×1920.
SIZES: dict[str, tuple[int, int]] = {
    "instagram_square": (1080, 1080),
    "instagram_portrait": (1080, 1350),
    "story": (1080, 1920),
    "twitter_landscape": (1600, 900),
    "linkedin": (1200, 627),
    "a4_portrait": (1240, 1754),
    "a4_landscape": (1754, 1240),
}

SIZE_LABELS = {
    "instagram_square": "Instagram post, square (1080 x 1080)",
    "instagram_portrait": "Instagram post, portrait (1080 x 1350)",
    "story": "Story or Reel (1080 x 1920)",
    "twitter_landscape": "X / Twitter (1600 x 900)",
    "linkedin": "LinkedIn (1200 x 627)",
    "a4_portrait": "A4 portrait, print (1240 x 1754)",
    "a4_landscape": "A4 landscape, print (1754 x 1240)",
    "custom": "Custom width and height",
}

#: Hosts a rendered page may load fonts from. Anything else is blocked at the
#: browser, so a page cannot be used to probe the network it renders in — the
#: same rule the issue-screenshot fetcher follows.
ALLOWED_RESOURCE_HOSTS = (
    "fonts.googleapis.com",
    "fonts.gstatic.com",
)

RENDER_TIMEOUT_SECONDS = 45

#: Under this the picture is one flat colour: a page that failed to lay out,
#: or type in white on white. Same threshold the video path uses, because it
#: is the same question asked of the same kind of frame.
FLAT_IMAGE_STDDEV = 4.0

INSTRUCTIONS = """You write a single HTML page that a headless browser
screenshots. There is no JavaScript: the page is rendered with scripting
disabled, so anything you write in a <script> tag never runs.

Rules, all of them load-bearing:

- Return ONE complete HTML document and nothing else. No explanation, no code
  fence, no markdown.
- Inline every style in one <style> block. There is no external stylesheet.
- The page is exactly the size given below. Set html and body to that width
  and height, margin 0, overflow hidden. Nothing may be cut off at the edge.
- Fonts: a Google Fonts <link> is allowed and nothing else. Always give a real
  fallback stack, because a font that fails to load must not collapse the
  layout.
- No network images. If photographs are provided they are named p0.png, p1.png
  and so on; use those exact names in <img src="p0.png">.
- Real contrast. Type must be legible against what is behind it: a dark scrim
  over a photograph, not white text hoping for a dark corner.
- Fill the frame. Empty space is a design decision, but a page that is 80
  percent background because the content did not stretch is a mistake.
- Set the copy in a hierarchy: one thing is the headline, everything else is
  smaller than it.
"""


class AiImageConfig(BaseModel):
    """Describe the picture; the model writes the page and this renders it."""

    model_config = {"extra": "forbid"}

    brief: str = Field(
        min_length=1,
        max_length=8000,
        title="What to make",
        description=(
            "The picture in words: what it says, what it is for. "
            "Supports {{ references }}, for example {{ nodes.writer.output.text }}."
        ),
    )
    style: str = Field(
        default="",
        max_length=2000,
        title="Art direction",
        description="Colours, mood, brand, the font you like. Optional.",
    )
    size: Literal[
        "instagram_square",
        "instagram_portrait",
        "story",
        "twitter_landscape",
        "linkedin",
        "a4_portrait",
        "a4_landscape",
        "custom",
    ] = Field(
        default="instagram_square",
        title="Size",
        json_schema_extra={"x-enum-labels": SIZE_LABELS},
    )
    width: int = Field(default=0, ge=0, le=4000, description="Only used when size is custom.")
    height: int = Field(default=0, ge=0, le=4000, description="Only used when size is custom.")

    #: Photographs the page may show, as artifact ids or a reference, usually
    #: {{ trigger.photo_ids }} or the output of an earlier node.
    photos: str = Field(
        default="",
        title="Photos",
        max_length=4_000,
        description="Artifact ids of photos to use. They become p0.png, p1.png inside the page.",
    )

    provider: str = Field(default="anthropic", max_length=48)
    model: str = Field(default="", max_length=160)
    credential_id: str = Field(
        default="",
        title="Model credential",
        description="The saved key the model is called with.",
    )
    max_output_tokens: int = Field(
        default=8_000,
        ge=2_000,
        le=32_000,
        title="Maximum model output",
        description="Output budget for the page. Raise it if a model truncates the HTML.",
    )
    #: How many times the model may revise before this gives up. Each round is
    #: one model call and one screenshot, both cheap.
    max_attempts: int = Field(
        default=2,
        ge=1,
        le=4,
        title="Tries",
        description="How many times the model may fix its own page before this gives up.",
    )

    #: 2 is a retina render — the same layout at twice the pixels. Print wants
    #: 3; a Telegram photo does not.
    scale: int = Field(default=2, ge=1, le=4, title="Pixel density")
    format: Literal["png", "jpeg"] = "png"
    jpeg_quality: int = Field(default=90, ge=40, le=100)
    filename: str = Field(default="image", max_length=100)

    @model_validator(mode="after")
    def _custom_needs_dimensions(self) -> AiImageConfig:
        if self.size == "custom" and not (self.width and self.height):
            raise ValueError("A custom size needs both width and height.")
        return self

    def dimensions(self) -> tuple[int, int]:
        if self.size == "custom":
            return self.width, self.height
        return SIZES[self.size]


class AiImageNode(Node):
    """Brief in, finished picture out, with the model's revisions on the log."""

    type = "image.ai"
    label = "AI Image"
    description = "Describe the picture. A model writes the page, a browser renders it to PNG."
    when = (
        "A poster, a card, a quote graphic, an announcement, a thumbnail. Anything where the "
        "words have to be exactly right, which is where image models fail."
    )
    needs = (
        (
            "An LLM credential (OpenAI, Anthropic, Gemini, Groq or another provider) saved under "
            "Credentials"
        ),
        "Photos from the trigger or an earlier node, when the picture should show them.",
    )
    example = "Schedule -> Generate with LLM -> AI Image -> Post to Social Media"
    tier = 2
    category = "design"
    config_model = AiImageConfig
    output_paths = (
        "artifact_id",
        "url",
        "width",
        "height",
        "size_bytes",
        "attempts",
        "html",
        "usage.input_tokens",
        "usage.output_tokens",
        "usage.cost_usd",
    )

    #: Headless Chromium with real fonts. Cheaper than a video, still the kind
    #: of work that should not run four-up on two cores.
    heavy: ClassVar[bool] = True
    #: One attempt at the node level: the revision loop inside already retries
    #: the part that fails, and a second run would pay for the model twice.
    max_attempts = 1
    timeout_seconds = 300.0

    async def run(self, config: AiImageConfig, ctx: NodeContext) -> NodeResult:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        from basivo_orch.flows.nodes.models import build_chat_model, price_of

        if ctx.save_artifact is None:  # pragma: no cover - engine contract
            raise NodeError("This run cannot save generated images.")

        template_context = ctx.template_context()
        brief = str(render_value(config.brief, template_context)).strip()
        if not brief:
            raise NodeError(
                "The brief rendered empty, so there is nothing to draw. Check its reference "
                "against the output of the node before this one."
            )
        style = str(render_value(config.style, template_context)) if config.style else ""
        width, height = config.dimensions()

        photos = await _load_photos(config.photos, ctx)
        model = await build_chat_model(
            ctx,
            provider=config.provider,
            model=config.model,
            credential_id=config.credential_id,
            max_tokens=config.max_output_tokens,
            temperature=0.4,
        )
        usage = {"input_tokens": 0, "output_tokens": 0}

        instructions = f"{INSTRUCTIONS}\n\nThis page is exactly {width} by {height} pixels." + (
            "\n\nPHOTOGRAPHS ARE PROVIDED. Use these exact names and no others: "
            + ", ".join(sorted(photos))
            + ". Cover the space they sit in (object-fit: cover) and never stretch a face."
            if photos
            else ""
        )
        request = f"Make this:\n\n{brief}" + (f"\n\nArt direction:\n{style}" if style else "")

        conversation: list[Any] = [
            SystemMessage(content=instructions),
            HumanMessage(content=request),
        ]

        image = b""
        html = ""
        attempt = 0
        problems: list[str] = []
        while attempt < config.max_attempts:
            attempt += 1
            await ctx.progress(
                f"Writing the page ({attempt} of {config.max_attempts})"
                if attempt > 1
                else "Writing the page"
            )
            reply = await model.ainvoke(conversation)
            _add_usage(reply, usage)
            html = _html_of(message_text_of(reply))
            if not html:
                problems = ["The reply contained no HTML document."]
                conversation += [
                    AIMessage(content=message_text_of(reply)),
                    HumanMessage(content="Return one complete HTML document and nothing else."),
                ]
                continue

            await ctx.step("image.page", {"attempt": attempt, "characters": len(html)})
            await ctx.progress(f"Rendering {width}×{height} at {config.scale}x")
            image = await _screenshot(
                _inline_photos(html, photos), width=width, height=height, config=config
            )

            problems = _problems(image)
            if not problems:
                break
            await ctx.step("image.rejected", {"attempt": attempt, "problems": problems})
            conversation += [
                AIMessage(content=html),
                HumanMessage(
                    content=(
                        "The rendered picture is wrong. Fix every problem below and return the "
                        "whole HTML document again:\n" + "\n".join(f"- {p}" for p in problems)
                    )
                ),
            ]

        if problems:
            raise NodeError(
                "The model could not produce a picture worth keeping after "
                f"{attempt} tries: {' '.join(problems)} Give the brief something concrete to "
                "put on the page, or raise Tries."
            )

        saved = await ctx.save_artifact(
            image,
            filename=f"{config.filename}.{config.format}",
            content_type=f"image/{config.format}",
            node_id=ctx.node_id,
        )
        cost = price_of(
            model=config.model,
            provider=config.provider,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
        )
        await ctx.step(
            "image.finished",
            {**saved, "width": width, "height": height, "attempts": attempt},
        )

        return NodeResult(
            output={
                **saved,
                "width": width,
                "height": height,
                "attempts": attempt,
                "html": html,
                "usage": {**usage, "cost_usd": round(cost or 0.0, 6)},
            },
            metrics={
                "tokens_in": usage["input_tokens"],
                "tokens_out": usage["output_tokens"],
                "cost_usd": round(cost or 0.0, 6),
            },
        )


def _add_usage(reply: Any, usage: dict[str, int]) -> None:
    counts = getattr(reply, "usage_metadata", None) or {}
    usage["input_tokens"] += int(counts.get("input_tokens") or 0)
    usage["output_tokens"] += int(counts.get("output_tokens") or 0)


def _html_of(text: str) -> str:
    """The document out of a reply, however the model wrapped it."""
    body = strip_code_fences(text).strip()
    if "<" not in body:
        return ""
    # Models add a sentence before the document about one time in five even
    # when told not to, and that sentence renders as a line of stray text at
    # the top of the picture.
    match = re.search(r"(?is)<!doctype html.*|<html.*", body)
    return (match.group(0) if match else body).strip()


def _problems(image: bytes) -> list[str]:
    """What is wrong with the picture that came out."""
    if not image:
        return ["Nothing rendered."]
    if frame_spread(image) < FLAT_IMAGE_STDDEV:
        return [
            "The picture is one flat colour. Nothing is visible: either the content is the "
            "same colour as the background, or the layout put it outside the page."
        ]
    return []


async def _load_photos(reference: str, ctx: NodeContext) -> dict[str, bytes]:
    """The photographs the page may show, named p0, p1, p2."""
    if not reference.strip():
        return {}
    wanted = photo_ids(render_value(reference, ctx.template_context()))
    photos: dict[str, bytes] = {}
    for index, artifact_id in enumerate(wanted[:MAX_IMAGES]):
        if ctx.load_artifact and (blob := await ctx.load_artifact(artifact_id)):
            photos[f"p{index}.png"] = blob
    await ctx.step("image.photos", {"count": len(photos), "asked_for": len(wanted)})
    return photos


def _inline_photos(html: str, photos: dict[str, bytes]) -> str:
    """Photographs go into the page as data, not as a fetch.

    The renderer has no network beyond fonts, so `<img src="p0.png">` would be
    a broken image. Inlining also means the page cannot be handed a different
    picture than the flow chose.
    """
    for name, blob in photos.items():
        encoded = base64.b64encode(blob).decode("ascii")
        html = html.replace(name, f"data:image/png;base64,{encoded}")
    return html


async def _screenshot(html: str, *, width: int, height: int, config: AiImageConfig) -> bytes:
    """Screenshot one page. Raises NodeError with something actionable."""
    try:
        image = await asyncio.wait_for(
            _render(html, width=width, height=height, config=config),
            timeout=RENDER_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise NodeError(
            f"The page did not finish rendering within {RENDER_TIMEOUT_SECONDS}s. "
            "A web font that never loads is the usual cause."
        ) from exc
    return image


async def _render(html: str, *, width: int, height: int, config: AiImageConfig) -> bytes:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise NodeError(
            "Rendering needs Playwright and a browser. Install it with "
            "`uv run playwright install chromium`."
        ) from exc

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                args=["--no-sandbox", "--font-render-hinting=none"]
            )
        except Exception as exc:  # noqa: BLE001 — the message matters more than the type
            raise NodeError(
                "Could not start the browser used for rendering. On a fresh install run "
                f"`uv run playwright install chromium`. ({exc})"
            ) from exc

        try:
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=config.scale,
                # Deterministic output beats matching the viewer's locale: two
                # runs of the same flow must produce the same file.
                locale="en-GB",
                timezone_id="UTC",
                java_script_enabled=False,
            )
            page = await context.new_page()

            async def gate(route: Any) -> None:
                url = route.request.url
                if url.startswith("data:") or url.startswith("about:"):
                    await route.continue_()
                    return
                host = url.split("/")[2] if "//" in url else ""
                if any(host.endswith(allowed) for allowed in ALLOWED_RESOURCE_HOSTS):
                    await route.continue_()
                    return
                await route.abort()

            await page.route("**/*", gate)
            await page.set_content(html, wait_until="load")
            # Without this the screenshot can land before a web font does,
            # producing a poster in the fallback face — which looks fine, and
            # is not the design anyone approved.
            await page.evaluate("document.fonts && document.fonts.ready")

            shot: dict[str, Any] = {"type": config.format}
            if config.format == "jpeg":
                shot["quality"] = config.jpeg_quality
            return await page.screenshot(**shot)
        finally:
            await browser.close()
