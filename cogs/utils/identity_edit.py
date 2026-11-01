"""Shared pieces for Krea 2 Identity Edit jobs.

``/imagegen edit`` and ``/halloweenify`` both size outputs with
``identity_edit_output_size``. A future ``/imagegen remix`` can reuse the
rest without a new queue path:

1. ``resolve_identity_edit_source(interaction, attachment)`` ->
   ``(bytes, filename, width, height)`` or a user-facing error string.
2. ``build_identity_edit_creation(source, interaction, prompt, ...)``.
3. ``enqueue_identity_edit(cog, interaction, image, queued_message)``.

``prepare_identity_edit_bytes`` is the offline version of step 1 for an
attachment that is already in memory (no Discord, no network).
"""

from __future__ import annotations

import logging
from io import BytesIO

from PIL import Image as PILImage
from PIL import ImageDraw

from cogs.utils.image_models import ImageCreation, Models, NsfwLevel

log = logging.getLogger(__name__)

AVATAR_EDIT_SIZE = 1024
IDENTITY_EDIT_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

AVATAR_FETCH_ERROR = "I couldn't fetch your avatar, so nothing was queued."
ATTACHMENT_TYPE_ERROR = "Please attach a PNG, JPG, WEBP, or GIF."
ATTACHMENT_TOO_BIG_ERROR = "That image is over 10 MB. Please attach a smaller file."
ATTACHMENT_EMPTY_ERROR = "That attachment is empty."
ATTACHMENT_DECODE_ERROR = "Could not read that image. Attach a PNG, JPG, WEBP, or GIF."
GIF_DECODE_ERROR = "Could not read the first frame of that GIF."

_ALLOWED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_ALLOWED_CONTENT_TYPES = frozenset({
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
})
_GENERIC_CONTENT_TYPES = frozenset({
    "",
    "application/octet-stream",
    "binary/octet-stream",
})
_RASTER_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


def identity_edit_output_size(width: int, height: int) -> tuple[int, int]:
    """Output size near 1MP, aspect preserved, both sides multiples of 64.

    This is the sizing ``/imagegen edit`` used to do inline. Non-positive
    sides fall back to 1024×1024. A very wide or tall image can stay a sliver
    over the ~2MP cap because each side floors at 64; that matches edit.
    """
    sw = int(width)
    sh = int(height)
    if sw <= 0 or sh <= 0:
        return 1024, 1024
    target_mp = 1_048_576  # ~1MP
    scale = (target_mp / (sw * sh)) ** 0.5
    edit_w = max(64, int(sw * scale) // 64 * 64)
    edit_h = max(64, int(sh * scale) // 64 * 64)
    # Cap ~2MP training range.
    if edit_w * edit_h > 2_000_000:
        scale2 = (2_000_000 / (edit_w * edit_h)) ** 0.5
        edit_w = max(64, int(edit_w * scale2) // 64 * 64)
        edit_h = max(64, int(edit_h * scale2) // 64 * 64)
    return edit_w, edit_h


def static_png_avatar(asset, size: int = AVATAR_EDIT_SIZE):
    """PNG of a display avatar at ``size``. Animated avatars use the still frame.

    ``replace(static_format=...)`` leaves an animated asset on its gif URL.
    ``format='png'`` asks Discord for the first frame.
    """
    if asset.is_animated():
        return asset.replace(size=size, format="png")
    return asset.replace(size=size, static_format="png")


def _content_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _extension(filename: str | None) -> str:
    name = (filename or "").replace("\\", "/").split("/")[-1].strip().lower()
    dot = name.rfind(".")
    if dot < 0:
        return ""
    return name[dot:]


def _basename(filename: str | None) -> str:
    name = (filename or "").strip().replace("/", "_").replace("\\", "_")
    name = name.split("/")[-1]
    return name.strip()


def attachment_error_message(
    *,
    filename: str | None,
    content_type: str | None,
    size: int | None,
    max_bytes: int = IDENTITY_EDIT_MAX_ATTACHMENT_BYTES,
) -> str | None:
    """Return a user-facing error, or None when the attachment metadata is ok.

    Accept an allowed content type, or an allowed extension when the content
    type is missing or a generic octet-stream. Reject other types, empty
    files, and anything over ``max_bytes``.
    """
    if size is not None and size > max_bytes:
        return ATTACHMENT_TOO_BIG_ERROR
    if size is not None and size <= 0:
        return ATTACHMENT_EMPTY_ERROR
    ctype = _content_type(content_type)
    ext = _extension(filename)
    type_ok = ctype in _ALLOWED_CONTENT_TYPES
    ext_ok = ext in _ALLOWED_EXTENSIONS
    if type_ok or (ext_ok and ctype in _GENERIC_CONTENT_TYPES):
        return None
    return ATTACHMENT_TYPE_ERROR


def _is_gif(content_type: str | None, filename: str | None) -> bool:
    """True when the content type or the filename says this is a GIF."""
    return _content_type(content_type) == "image/gif" or _extension(filename) == ".gif"


def _as_png_name(filename: str | None) -> str:
    name = _basename(filename) or "discord_edit"
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if not stem or stem in {".", ".."}:
        stem = "discord_edit"
    return f"{stem}.png"


def _safe_upload_name(filename: str | None) -> str:
    name = _basename(filename) or "discord_edit.png"
    if name in {".", ".."}:
        name = "discord_edit.png"
    if not name.lower().endswith(_RASTER_EXTENSIONS):
        return f"{name}.png"
    return name


def gif_first_frame_png(data: bytes) -> bytes:
    """PNG bytes of the first GIF frame."""
    with PILImage.open(BytesIO(data)) as im:
        im.seek(0)
        frame = im.convert("RGBA")
        out = BytesIO()
        frame.save(out, format="PNG")
        return out.getvalue()


def prepare_identity_edit_bytes(
    data: bytes,
    *,
    filename: str | None,
    content_type: str | None,
) -> tuple[bytes, str, int, int] | str:
    """Normalize attachment bytes for an identity-edit job.

    Returns ``(bytes, upload filename, width, height)`` sized near 1MP, or a
    user-facing error string. GIF input becomes a PNG of the first frame.
    Other allowed types keep their bytes; only the output size changes.
    """
    if not data:
        return ATTACHMENT_EMPTY_ERROR
    try:
        if _is_gif(content_type, filename):
            try:
                data = gif_first_frame_png(data)
            except Exception:
                log.warning("Could not decode GIF attachment %r", filename)
                return GIF_DECODE_ERROR
            filename = _as_png_name(filename)
        else:
            filename = _safe_upload_name(filename)
        with PILImage.open(BytesIO(data)) as im:
            width, height = im.size
    except Exception:
        log.warning("Could not decode identity-edit attachment %r", filename)
        return ATTACHMENT_DECODE_ERROR
    out_w, out_h = identity_edit_output_size(width, height)
    return data, filename, out_w, out_h


async def _source_from_avatar(interaction, *, filename: str, size: int):
    user = getattr(interaction, "user", None)
    avatar = getattr(user, "display_avatar", None)
    if avatar is None:
        return AVATAR_FETCH_ERROR
    try:
        asset = static_png_avatar(avatar, size=size)
        data = await asset.read()
    except Exception:
        log.exception(
            "Failed to fetch avatar for identity edit user=%s",
            getattr(user, "id", None),
        )
        return AVATAR_FETCH_ERROR
    if not data:
        return AVATAR_FETCH_ERROR
    return data, filename, size, size


async def _source_from_attachment(attachment, *, max_bytes: int):
    filename = getattr(attachment, "filename", None)
    content_type = getattr(attachment, "content_type", None)
    size = getattr(attachment, "size", None)
    error = attachment_error_message(
        filename=filename,
        content_type=content_type,
        size=size,
        max_bytes=max_bytes,
    )
    if error:
        return error
    try:
        data = await attachment.read()
    except Exception as exc:
        log.exception("Failed to read Discord attachment")
        return f"Could not read the attached image: {exc}"
    if not data:
        return ATTACHMENT_EMPTY_ERROR
    if len(data) > max_bytes:
        return ATTACHMENT_TOO_BIG_ERROR
    return prepare_identity_edit_bytes(data, filename=filename, content_type=content_type)


async def resolve_identity_edit_source(
    interaction,
    attachment=None,
    *,
    avatar_filename: str = "avatar.png",
    avatar_size: int = AVATAR_EDIT_SIZE,
    max_bytes: int = IDENTITY_EDIT_MAX_ATTACHMENT_BYTES,
) -> tuple[bytes, str, int, int] | str:
    """Resolve the image an identity-edit command should run on.

    An attachment wins over the caller's avatar. Avatars stay square at
    ``avatar_size`` (default 1024) and are not run through the ~1MP scaler.
    Returns ``(bytes, filename, width, height)`` or a user-facing error string.
    """
    if attachment is not None:
        return await _source_from_attachment(attachment, max_bytes=max_bytes)
    return await _source_from_avatar(
        interaction,
        filename=avatar_filename,
        size=avatar_size,
    )


def build_identity_edit_creation(
    source: tuple[bytes, str, int, int],
    interaction,
    prompt: str,
    *,
    caption_label: str = "",
    retain_source: bool = False,
    preset: str = "",
    details: str = "",
    edit_strength: float | None = None,
) -> ImageCreation:
    """Build a Krea 2 Identity Edit ``ImageCreation``.

    ``caption_label`` is the public caption prefix, for example
    ``**Halloweenify** (Vampire)``. ``preset`` and ``details`` are shown on
    the prompt-details button. ``retain_source`` keeps the bytes so Regenerate
    can run again. ``edit_strength``, when set, is the conservative ref_boost
    the workflow should use so the edit keeps more of the source.
    """
    data, filename, width, height = source
    if not data:
        raise ValueError("identity edit requires source image bytes")
    return ImageCreation(
        prompt,
        Models.KREA2_IDENTITY_EDIT,
        interaction=interaction,
        negative_prompt="",
        use_default_negative=False,
        use_default_positive=False,
        nsfw=NsfwLevel.NOT_SPECIFIED,
        llm_prompt_enhance=False,
        source_image_bytes=data,
        source_image_filename=filename or "discord_edit.png",
        edit_width=width,
        edit_height=height,
        retain_source=retain_source,
        caption_label=caption_label,
        prompt_preset=preset,
        prompt_details=details,
        edit_strength=edit_strength,
    )


# Local /halloweenify borders. Drawn with Pillow, no AI. Keys are slash-command values.
HALLOWEEN_FRAMES: dict[str, str] = {
    "pumpkin": "Pumpkin border",
    "vines": "Spooky vines",
    "bats": "Bats",
    "candy_corn": "Candy corn",
    "orange_black": "Orange and black",
}
_FRAME_ALIASES = {
    "pumpkin_border": "pumpkin",
    "spooky_vines": "vines",
    "candycorn": "candy_corn",
    "orange_and_black": "orange_black",
    "plain": "orange_black",
    "plain_orange_black": "orange_black",
}


def normalize_halloween_frame(frame: str | None) -> str | None:
    """Return a frame key, None when no frame was chosen, or raise ValueError."""
    if frame is None:
        return None
    text = str(frame).strip().lower()
    if text in {"", "none"}:
        return None
    key = text.replace("-", " ").replace("_", " ")
    key = "_".join(key.split())
    key = _FRAME_ALIASES.get(key, key)
    if key not in HALLOWEEN_FRAMES:
        raise ValueError(f"unknown halloween frame: {frame}")
    return key


def _frame_band(size: tuple[int, int]) -> int:
    short = min(size)
    if short <= 2:
        return 1
    return max(2, min(28, short // 12))


def _stroke_border(draw: ImageDraw.ImageDraw, size: tuple[int, int], color: tuple[int, int, int, int], width: int) -> None:
    width = max(1, min(width, size[0], size[1]))
    image_w, image_h = size
    for inset in range(width):
        draw.rectangle((inset, inset, image_w - 1 - inset, image_h - 1 - inset), outline=color)


def _paint_pumpkin(draw: ImageDraw.ImageDraw, size: tuple[int, int]) -> None:
    band = _frame_band(size)
    _stroke_border(draw, size, (255, 128, 0, 255), band)
    image_w, image_h = size
    inner = min(band, max(0, min(image_w, image_h) // 2 - 1))
    if inner > 0 and image_w > inner * 2 and image_h > inner * 2:
        draw.rectangle((inner, inner, image_w - 1 - inner, image_h - 1 - inner), outline=(20, 10, 0, 255))
    radius = max(3, band)
    centers = (
        (radius, radius),
        (image_w - 1 - radius, radius),
        (radius, image_h - 1 - radius),
        (image_w - 1 - radius, image_h - 1 - radius),
    )
    for cx, cy in centers:
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=(255, 140, 0, 255),
            outline=(30, 12, 0, 255),
        )
        draw.polygon(
            [(cx, cy - radius), (cx - 3, cy - radius + 4), (cx + 3, cy - radius + 4)],
            fill=(30, 120, 40, 255),
        )


def _paint_vines(draw: ImageDraw.ImageDraw, size: tuple[int, int]) -> None:
    band = _frame_band(size)
    dark = (12, 48, 24, 255)
    leaf = (36, 140, 52, 255)
    _stroke_border(draw, size, dark, max(2, band // 2))
    image_w, image_h = size
    step = max(6, band)
    for x in range(0, image_w, step):
        right = min(image_w - 1, x + step)
        mid = min(image_w - 1, x + step // 2)
        draw.line([(x, 1), (mid, min(image_h - 1, band)), (right, 1)], fill=leaf, width=2)
        bottom = image_h - 1
        draw.line(
            [(x, bottom - 1), (mid, max(0, bottom - band)), (right, bottom - 1)],
            fill=leaf,
            width=2,
        )
    for y in range(0, image_h, step):
        low = min(image_h - 1, y + step)
        mid = min(image_h - 1, y + step // 2)
        draw.line([(1, y), (min(image_w - 1, band), mid), (1, low)], fill=leaf, width=2)
        edge = image_w - 1
        draw.line(
            [(edge - 1, y), (max(0, edge - band), mid), (edge - 1, low)],
            fill=leaf,
            width=2,
        )


def _paint_bats(draw: ImageDraw.ImageDraw, size: tuple[int, int]) -> None:
    black = (8, 8, 12, 255)
    _stroke_border(draw, size, black, max(2, _frame_band(size) // 2))
    image_w, image_h = size

    def bat(cx: int, cy: int, scale: int) -> None:
        span = max(4, scale)
        draw.polygon(
            [
                (cx - span, cy),
                (cx - span // 2, cy - span // 2),
                (cx, cy - max(1, span // 6)),
                (cx + span // 2, cy - span // 2),
                (cx + span, cy),
                (cx + span // 3, cy + max(1, span // 5)),
                (cx, cy),
                (cx - span // 3, cy + max(1, span // 5)),
            ],
            fill=black,
        )

    scale = max(6, min(image_w, image_h) // 10)
    bat(scale, scale, scale)
    bat(max(scale, image_w - 1 - scale), scale, scale)
    bat(image_w // 2, scale, scale)


def _paint_candy_corn(draw: ImageDraw.ImageDraw, size: tuple[int, int]) -> None:
    band = _frame_band(size)
    colors = (
        (255, 255, 255, 255),
        (255, 150, 20, 255),
        (255, 214, 40, 255),
    )
    image_w, image_h = size
    for inset in range(band):
        color = colors[min(len(colors) - 1, (inset * len(colors)) // max(1, band))]
        draw.rectangle((inset, inset, image_w - 1 - inset, image_h - 1 - inset), outline=color)


def _paint_orange_black(draw: ImageDraw.ImageDraw, size: tuple[int, int]) -> None:
    band = _frame_band(size)
    half = max(1, band // 2)
    _stroke_border(draw, size, (0, 0, 0, 255), half)
    image_w, image_h = size
    for inset in range(half, band):
        draw.rectangle((inset, inset, image_w - 1 - inset, image_h - 1 - inset), outline=(255, 120, 0, 255))


_FRAME_PAINTERS = {
    "pumpkin": _paint_pumpkin,
    "vines": _paint_vines,
    "bats": _paint_bats,
    "candy_corn": _paint_candy_corn,
    "orange_black": _paint_orange_black,
}


def apply_halloween_frame(image: PILImage.Image, frame: str) -> PILImage.Image:
    """Draw a Halloween border on a copy. Width and height stay the same."""
    key = normalize_halloween_frame(frame)
    if key is None:
        raise ValueError(f"unknown halloween frame: {frame}")
    painter = _FRAME_PAINTERS[key]
    base = image.convert("RGBA")
    if base is image:
        base = image.copy()
    overlay = PILImage.new("RGBA", base.size, (0, 0, 0, 0))
    painter(ImageDraw.Draw(overlay), base.size)
    return PILImage.alpha_composite(base, overlay)


def render_halloween_frame_png(data: bytes, frame: str) -> bytes:
    """PNG bytes of ``data`` with a local Halloween frame. No AI."""
    with PILImage.open(BytesIO(data)) as image:
        framed = apply_halloween_frame(image, frame)
    out = BytesIO()
    framed.save(out, format="PNG")
    return out.getvalue()


async def enqueue_identity_edit(
    cog,
    interaction,
    image: ImageCreation,
    queued_message: str,
    *,
    ephemeral: bool = False,
) -> bool:
    """Put an identity-edit job on the cog queue and send ``queued_message``."""
    return await cog.enqueue_image(
        image,
        interaction,
        queued_message=queued_message,
        ephemeral=ephemeral,
    )
