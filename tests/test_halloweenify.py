"""Halloweenify: costume presets, optional details, avatar or attachment. No network."""

from __future__ import annotations

import asyncio
import random
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.asset import Asset
from PIL import Image as PILImage

from cogs.imagediffusion import (
    HALLOWEENIFY_BASE_PROMPT,
    HALLOWEENIFY_COSTUMES,
    HALLOWEENIFY_DETAILS_MAX,
    HALLOWEENIFY_EDIT_STRENGTH,
    HALLOWEENIFY_ENABLED,
    HALLOWEENIFY_PRESET_ORDER,
    HALLOWEENIFY_SIZE,
    HALLOWEENIFY_STYLE_PRESERVATION,
    QUEUE_FULL_MESSAGE,
    ImageDiffusion,
    PromptDetailButton,
    app_commands_for_tree,
    build_halloweenify_image,
    build_halloweenify_prompt,
    generation_caption,
    halloweenify_preset_label,
    halloweenify_result_files,
    release_source_bytes,
    sanitize_halloween_details,
    static_png_avatar,
)
from cogs.utils.identity_edit import (
    ATTACHMENT_TOO_BIG_ERROR,
    ATTACHMENT_TYPE_ERROR,
    HALLOWEEN_FRAMES,
    IDENTITY_EDIT_MAX_ATTACHMENT_BYTES,
    apply_halloween_frame,
    attachment_error_message,
    identity_edit_output_size,
    prepare_identity_edit_bytes,
    render_halloween_frame_png,
)
from cogs.utils.image_models import ComfyKrea2IdentityEditModel, ImageCreation, Models, NsfwLevel

_GUARDRAILS = (
    "same subject",
    "identity",
    "face",
    "pose",
    "composition",
    "recognizable and fun",
    "jack-o'-lanterns",
    "bats",
    "full moon",
    "Not gory",
    "no blood",
    "no nudity",
)


class FakeAvatar:
    def __init__(self, data: bytes, *, animated: bool = False, fail: bool = False):
        self.data = data
        self.animated = animated
        self.fail = fail
        self.kwargs = None

    def is_animated(self) -> bool:
        return self.animated

    def replace(self, **kwargs):
        self.kwargs = kwargs
        return self

    async def read(self) -> bytes:
        if self.fail:
            raise RuntimeError("cdn down")
        return self.data


class FakeAttachment:
    def __init__(
        self,
        data: bytes,
        *,
        filename: str = "pic.png",
        content_type: str = "image/png",
        size: int | None = -1,
        fail_read: bool = False,
    ):
        self._data = data
        self.filename = filename
        self.content_type = content_type
        # size=-1 means "Discord told us the byte length". None means unknown.
        self.size = len(data) if size == -1 else size
        self.fail_read = fail_read
        self.read_called = False

    async def read(self) -> bytes:
        self.read_called = True
        if self.fail_read:
            raise RuntimeError("cdn down")
        return self._data


class FakeDiffusion:
    def __init__(self, *, allowed: bool = True):
        self.allowed = allowed
        self.entries = []
        self.kwargs = None
        self.seen = None

    def _is_allowed_channel(self, guild_id: int, channel_id: int) -> bool:
        self.seen = (guild_id, channel_id)
        return self.allowed

    async def enqueue_image(self, entry, interaction, **kwargs):
        self.entries.append(entry)
        self.kwargs = kwargs
        await interaction.response.send_message(kwargs["queued_message"], ephemeral=False)
        return True


def _user(data: bytes, *, animated: bool = False, fail: bool = False, user_id: int = 7):
    return SimpleNamespace(
        id=user_id,
        mention=f"<@{user_id}>",
        display_avatar=FakeAvatar(data, animated=animated, fail=fail),
    )


def _interaction(user, *, guild_id=10, channel_id=20):
    return SimpleNamespace(
        user=user,
        guild_id=guild_id,
        channel_id=channel_id,
        target_user=SimpleNamespace(display_avatar=FakeAvatar(b"someone-else")),
        response=SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False, defer=AsyncMock()),
    )


def _png(width: int, height: int, color=(10, 20, 30)) -> bytes:
    image = PILImage.new("RGB", (width, height), color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg(width: int, height: int) -> bytes:
    image = PILImage.new("RGB", (width, height), (4, 5, 6))
    buffer = BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def _gif(size=(80, 40)) -> bytes:
    first = PILImage.new("RGB", size, (255, 0, 0))
    second = PILImage.new("RGB", size, (0, 0, 255))
    buffer = BytesIO()
    first.save(buffer, format="GIF", save_all=True, append_images=[second], duration=80, loop=0)
    return buffer.getvalue()


def _run(coro):
    asyncio.run(coro)


def test_every_preset_appends_its_costume_to_the_guardrails():
    assert len(HALLOWEENIFY_PRESET_ORDER) == 7
    assert "random" not in HALLOWEENIFY_PRESET_ORDER
    assert HALLOWEENIFY_COSTUMES["vampire"] == (
        "an elegant vampire with a high-collared cape, pale makeup, and small fangs"
    )
    for preset in HALLOWEENIFY_PRESET_ORDER:
        resolved, prompt = build_halloweenify_prompt(preset)
        description = HALLOWEENIFY_COSTUMES[preset]
        assert resolved == preset
        assert prompt.startswith(HALLOWEENIFY_BASE_PROMPT)
        assert f"Costume: {description}." in prompt
        assert prompt.endswith(HALLOWEENIFY_STYLE_PRESERVATION)
        assert "Extra details:" not in prompt
        for phrase in _GUARDRAILS:
            assert phrase in prompt
        titled, _ = build_halloweenify_prompt(preset.upper())
        assert titled == preset


def test_random_preset_is_deterministic_with_a_seeded_rng():
    expected = random.Random(12345).choice(HALLOWEENIFY_PRESET_ORDER)
    first = build_halloweenify_prompt("random", rng=random.Random(12345))
    second = build_halloweenify_prompt("random", None, random.Random(12345))
    assert first == second
    assert first[0] == expected
    assert first[0] in HALLOWEENIFY_PRESET_ORDER
    assert HALLOWEENIFY_COSTUMES[first[0]] in first[1]
    assert "Costume: random" not in first[1].lower()

    explicit, _ = build_halloweenify_prompt("witch", rng=random.Random(0))
    assert explicit == "witch"

    seen = {
        build_halloweenify_prompt("random", rng=random.Random(seed))[0]
        for seed in range(40)
    }
    assert seen <= set(HALLOWEENIFY_PRESET_ORDER)
    assert len(seen) >= 2

    try:
        build_halloweenify_prompt("clown")
    except ValueError as exc:
        assert "clown" in str(exc)
    else:
        raise AssertionError("unknown costumes should be rejected")


def test_sanitize_details_strips_mentions_controls_and_caps_length():
    assert sanitize_halloween_details(None) == ""
    assert sanitize_halloween_details("   \n\t  ") == ""
    assert sanitize_halloween_details("<@12> <@!34> <@&56> <#78> @everyone @here") == ""
    assert sanitize_halloween_details("ping @Everyone please @HERE now") == "ping please now"
    assert sanitize_halloween_details("see @hereafter") == "see @hereafter"
    assert sanitize_halloween_details("line\none\r\ntwo\tthree") == "line one two three"
    assert sanitize_halloween_details("a\x00b\x1fc\x7fd\x85e") == "a b c d e"

    nearby = ("token " * 30) + "TAILWORD" + ("x" * 30)
    clipped = sanitize_halloween_details(nearby)
    assert clipped == ("token " * 29) + "token"
    assert len(clipped) <= HALLOWEENIFY_DETAILS_MAX
    assert "TAIL" not in clipped

    hard = sanitize_halloween_details("b" * 250)
    assert hard == "b" * HALLOWEENIFY_DETAILS_MAX
    assert sanitize_halloween_details(hard) == hard

    far_space = "hi " + ("c" * 300)
    assert sanitize_halloween_details(far_space) == "hi " + ("c" * 197)

    _, omitted = build_halloweenify_prompt("ghost", details="<@1> @everyone")
    assert "Extra details:" not in omitted
    _, kept = build_halloweenify_prompt("ghost", details="  glowing\neyes  ")
    assert "Extra details: glowing eyes" in kept
    assert kept.endswith(HALLOWEENIFY_STYLE_PRESERVATION)


def test_command_options_are_costume_details_and_optional_image():
    command = ImageDiffusion.halloweenify
    assert command.name == "halloweenify"
    assert command.guild_only is True
    assert command.parent is None
    params = command.parameters
    assert [param.name for param in params] == ["frame", "costume", "details", "image"]
    frame, costume, details, image = params
    assert all(param.required is False for param in params)
    assert costume.default == "none"
    assert costume.choices[0].name == "None (frame only)"
    assert costume.choices[0].value == "none"
    assert [choice.value for choice in costume.choices] == [
        "none",
        "random",
        *HALLOWEENIFY_PRESET_ORDER,
    ]
    assert [choice.name for choice in costume.choices] == [
        "None (frame only)",
        "Random",
        "Vampire",
        "Witch",
        "Skeleton",
        "Zombie",
        "Pumpkin King",
        "Werewolf",
        "Ghost",
    ]
    assert frame.default is None
    assert [choice.value for choice in frame.choices] == list(HALLOWEEN_FRAMES)
    assert [choice.name for choice in frame.choices] == list(HALLOWEEN_FRAMES.values())
    assert details.default is None
    assert image.default is None
    assert image.type is discord.AppCommandOptionType.attachment
    names = {param.name for param in params}
    assert "user" not in names
    assert "member" not in names
    assert "prompt" not in names


def test_static_png_avatar_requests_1024_and_a_still_frame():
    still = Asset(
        state=None,
        url="https://cdn.discordapp.com/avatars/9/abc.webp",
        key="abc",
        animated=False,
    )
    still_out = static_png_avatar(still)
    assert still_out.url.split("?")[0].endswith("/abc.png")
    assert "size=1024" in still_out.url

    moving = Asset(
        state=None,
        url="https://cdn.discordapp.com/avatars/9/a_abc.gif",
        key="a_abc",
        animated=True,
    )
    moving_out = static_png_avatar(moving)
    assert moving_out.url.split("?")[0].endswith("/a_abc.png")
    assert "size=1024" in moving_out.url
    assert not moving_out.url.split("?")[0].endswith(".gif")
    assert HALLOWEENIFY_SIZE == 1024


def test_build_uses_identity_edit_and_keeps_bytes_for_regenerate():
    caller = b"caller-avatar"
    interaction = SimpleNamespace(user=SimpleNamespace(mention="<@7>", id=7))
    image = build_halloweenify_image(caller, interaction, "vampire", "tiny hat")
    assert isinstance(image, ImageCreation)
    assert image.prompt.startswith(HALLOWEENIFY_BASE_PROMPT)
    assert "Costume: an elegant vampire" in image.prompt
    assert "Extra details: tiny hat" in image.prompt
    assert image.prompt.endswith(HALLOWEENIFY_STYLE_PRESERVATION)
    assert image.edit_strength == HALLOWEENIFY_EDIT_STRENGTH
    assert image.model is Models.KREA2_IDENTITY_EDIT
    assert image.source_image_bytes is caller
    assert image.source_image_filename == "halloweenify_avatar.png"
    assert image.edit_width == 1024
    assert image.edit_height == 1024
    assert image.retain_source is True
    assert image.llm_prompt_enhance is False
    assert image.use_default_positive is False
    assert image.use_default_negative is False
    assert image.nsfw is NsfwLevel.NOT_SPECIFIED
    assert image.caption_label == "**Halloweenify** (Vampire)"
    assert image.prompt_preset == "Vampire"
    assert image.prompt_details == "tiny hat"
    assert image.interaction is interaction
    assert generation_caption(image, "preview") == "**Halloweenify** (Vampire) — <@7>"
    assert "tiny hat" not in generation_caption(image, "preview")
    assert "jack-o'-lantern" not in generation_caption(image, "preview")

    plain = ImageCreation(
        "edit this",
        Models.KREA2_IDENTITY_EDIT,
        interaction=SimpleNamespace(user=SimpleNamespace(mention="<@3>")),
        source_image_bytes=b"upload",
    )
    assert plain.retain_source is False
    assert plain.caption_label == ""
    release_source_bytes(plain)
    assert plain.source_image_bytes is None
    release_source_bytes(image)
    assert image.source_image_bytes is caller
    plain_caption = generation_caption(plain, "red jacket")
    assert plain_caption.startswith("**Edit:** red jacket")


def test_empty_avatar_is_rejected():
    try:
        build_halloweenify_image(b"", SimpleNamespace())
    except ValueError as exc:
        assert "avatar" in str(exc)
    else:
        raise AssertionError("empty avatar should be rejected")


def test_shared_sizing_is_one_megapixel_and_multiples_of_64():
    assert identity_edit_output_size(1920, 1080) == (1344, 768)
    assert identity_edit_output_size(832, 1152) == (832, 1152)
    assert identity_edit_output_size(800, 1120) == (832, 1152)
    assert identity_edit_output_size(1024, 1024) == (1024, 1024)
    assert identity_edit_output_size(1000, 1500) == (832, 1216)
    assert identity_edit_output_size(0, 500) == (1024, 1024)
    # Same 64px floor as the old inline /imagegen edit math, so a sliver can remain over ~2MP.
    assert identity_edit_output_size(10000, 10) == (31744, 64)
    for width, height in ((1920, 1080), (832, 1152), (800, 1120), (1000, 1500), (64, 64)):
        out_w, out_h = identity_edit_output_size(width, height)
        assert out_w % 64 == 0 and out_h % 64 == 0
        assert out_w >= 64 and out_h >= 64
        if width >= 256 and height >= 256:
            assert 0.7 * 1_048_576 <= out_w * out_h <= 1.3 * 1_048_576


def test_imagegen_edit_uses_the_shared_sizer(monkeypatch):
    import cogs.imagediffusion as imagediffusion

    seen = []
    real = imagediffusion.identity_edit_output_size

    def spy(width, height):
        seen.append((width, height))
        return real(width, height)

    monkeypatch.setattr(imagediffusion, "identity_edit_output_size", spy)

    async def run():
        cog = FakeDiffusion()
        interaction = _interaction(_user(b"unused"))
        wide = FakeAttachment(_png(1920, 1080), filename="wide.png", content_type="image/png")
        tall = FakeAttachment(_png(832, 1152), filename="tall.png", content_type="image/png")
        await ImageDiffusion.edit_image.callback(cog, interaction, wide, "paint the jacket red")
        interaction = _interaction(_user(b"unused"))
        await ImageDiffusion.edit_image.callback(cog, interaction, tall, "paint the jacket red")
        assert seen == [(1920, 1080), (832, 1152)]
        assert (cog.entries[0].edit_width, cog.entries[0].edit_height) == (1344, 768)
        assert (cog.entries[1].edit_width, cog.entries[1].edit_height) == (832, 1152)
        assert cog.entries[0].retain_source is False
        assert cog.entries[0].source_image_bytes == wide._data
        assert cog.entries[0].source_image_filename == "wide.png"
        gif = FakeAttachment(_gif(), filename="dance.gif", content_type="image/gif")
        interaction = _interaction(_user(b"unused"))
        await ImageDiffusion.edit_image.callback(cog, interaction, gif, "paint the jacket red")
        assert cog.entries[2].source_image_bytes == gif._data
        assert cog.entries[2].source_image_filename == "dance.gif.png"
        assert (cog.entries[2].edit_width, cog.entries[2].edit_height) == identity_edit_output_size(80, 40)

    _run(run())


def test_attachment_metadata_allows_png_jpeg_webp_and_gif_only():
    ok = attachment_error_message(filename="a.png", content_type="image/png", size=12)
    assert ok is None
    assert attachment_error_message(filename="a.jpg", content_type="image/jpeg", size=12) is None
    assert attachment_error_message(filename="a.jpeg", content_type="", size=12) is None
    assert attachment_error_message(filename="a.webp", content_type="image/webp", size=12) is None
    assert attachment_error_message(filename="a.gif", content_type="image/gif", size=12) is None
    assert attachment_error_message(
        filename="a.png", content_type="application/octet-stream", size=12
    ) is None
    assert attachment_error_message(filename="", content_type="image/png", size=12) is None
    assert attachment_error_message(
        filename="a.png", content_type="image/png", size=IDENTITY_EDIT_MAX_ATTACHMENT_BYTES
    ) is None
    assert attachment_error_message(filename="notes.txt", content_type="text/plain", size=4) == (
        ATTACHMENT_TYPE_ERROR
    )
    assert attachment_error_message(filename="a.png", content_type="text/plain", size=4) == (
        ATTACHMENT_TYPE_ERROR
    )
    assert attachment_error_message(filename="a.bmp", content_type="image/bmp", size=4) == (
        ATTACHMENT_TYPE_ERROR
    )
    assert attachment_error_message(
        filename="big.png",
        content_type="image/png",
        size=IDENTITY_EDIT_MAX_ATTACHMENT_BYTES + 1,
    ) == ATTACHMENT_TOO_BIG_ERROR


def test_prepare_keeps_jpeg_bytes_and_flattens_gif_to_png():
    jpeg = _jpeg(64, 64)
    prepared = prepare_identity_edit_bytes(jpeg, filename="shot.jpg", content_type="image/jpeg")
    assert prepared[0] == jpeg
    assert prepared[1] == "shot.jpg"
    assert prepared[2:] == identity_edit_output_size(64, 64)

    gif = _gif()
    prepared_gif = prepare_identity_edit_bytes(gif, filename="dance.gif", content_type="image/gif")
    assert isinstance(prepared_gif, tuple)
    data, filename, width, height = prepared_gif
    assert filename == "dance.png"
    assert (width, height) == identity_edit_output_size(80, 40)
    with PILImage.open(BytesIO(data)) as frame:
        assert frame.format == "PNG"
        assert frame.size == (80, 40)
        assert frame.convert("RGBA").getpixel((0, 0))[:3] == (255, 0, 0)


def test_halloweenify_queues_the_caller_avatar_by_default():
    async def run():
        caller = _user(b"caller-png")
        interaction = _interaction(caller)
        cog = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(cog, interaction, costume="vampire")

        assert cog.seen == (10, 20)
        assert len(cog.entries) == 1
        entry = cog.entries[0]
        assert entry.source_image_bytes == b"caller-png"
        assert entry.source_image_bytes != b"someone-else"
        assert entry.source_image_filename == "halloweenify_avatar.png"
        assert "Costume: an elegant vampire" in entry.prompt
        assert entry.model is Models.KREA2_IDENTITY_EDIT
        assert entry.edit_width == 1024 and entry.edit_height == 1024
        assert entry.retain_source is True
        assert entry.prompt_preset == "Vampire"
        assert caller.display_avatar.kwargs == {"size": 1024, "static_format": "png"}
        assert cog.kwargs["queued_message"] == "Queued a Halloween version of your avatar (1024×1024)."
        assert interaction.response.send_message.await_count == 1

    _run(run())


def test_attachment_beats_avatar_and_sizes_like_imagegen_edit():
    async def run():
        caller = _user(b"caller-png")
        interaction = _interaction(caller)
        cog = FakeDiffusion()
        attachment = FakeAttachment(_png(1920, 1080), filename="wide.png", content_type="image/png")
        await ImageDiffusion.halloweenify.callback(
            cog,
            interaction,
            costume="witch",
            details="<@9>\ntiny hat @everyone",
            image=attachment,
        )
        entry = cog.entries[0]
        assert caller.display_avatar.kwargs is None
        assert entry.source_image_bytes != b"caller-png"
        assert entry.source_image_bytes != b"someone-else"
        assert entry.source_image_filename == "wide.png"
        assert (entry.edit_width, entry.edit_height) == identity_edit_output_size(1920, 1080)
        assert entry.prompt_preset == "Witch"
        assert entry.prompt_details == "tiny hat"
        assert "<@" not in entry.prompt
        assert "@everyone" not in entry.prompt
        assert "Extra details: tiny hat" in entry.prompt
        assert entry.prompt.endswith(HALLOWEENIFY_STYLE_PRESERVATION)
        assert entry.edit_strength == HALLOWEENIFY_EDIT_STRENGTH
        assert cog.kwargs["queued_message"] == "Queued a Halloween version of your image (1344×768)."
        assert generation_caption(entry, "preview") == "**Halloweenify** (Witch) — <@7>"

        edit_cog = FakeDiffusion()
        edit_interaction = _interaction(_user(b"unused"))
        edit_attachment = FakeAttachment(_png(1920, 1080), filename="wide.png")
        await ImageDiffusion.edit_image.callback(
            edit_cog, edit_interaction, edit_attachment, "make the coat red"
        )
        assert (edit_cog.entries[0].edit_width, edit_cog.entries[0].edit_height) == (
            entry.edit_width,
            entry.edit_height,
        )

    _run(run())


def test_bad_attachment_is_ephemeral_and_not_queued():
    async def run():
        caller = _user(b"caller-png")
        cog = FakeDiffusion()
        interaction = _interaction(caller)
        notes = FakeAttachment(b"hello", filename="notes.txt", content_type="text/plain", size=5)
        await ImageDiffusion.halloweenify.callback(cog, interaction, costume="ghost", image=notes)
        assert cog.entries == []
        assert notes.read_called is False
        args = interaction.response.send_message.await_args
        assert args.args[0] == ATTACHMENT_TYPE_ERROR
        assert args.kwargs["ephemeral"] is True

        huge = FakeAttachment(
            b"x",
            filename="big.png",
            content_type="image/png",
            size=IDENTITY_EDIT_MAX_ATTACHMENT_BYTES + 1,
        )
        interaction = _interaction(caller)
        cog = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(cog, interaction, costume="ghost", image=huge)
        assert cog.entries == []
        assert huge.read_called is False
        assert interaction.response.send_message.await_args.args[0] == ATTACHMENT_TOO_BIG_ERROR
        assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True

        garbage = FakeAttachment(b"not-an-image", filename="x.png", content_type="image/png")
        interaction = _interaction(caller)
        cog = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(cog, interaction, costume="ghost", image=garbage)
        assert cog.entries == []
        assert "Could not read" in interaction.response.send_message.await_args.args[0]

    _run(run())


def test_gif_attachment_uses_the_first_frame_as_png():
    async def run():
        gif = _gif()
        attachment = FakeAttachment(gif, filename="dance.gif", content_type="image/gif")
        interaction = _interaction(_user(b"caller-png"))
        cog = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(
            cog, interaction, costume="pumpkin king", image=attachment
        )
        entry = cog.entries[0]
        assert entry.source_image_filename == "dance.png"
        assert entry.source_image_bytes != gif
        assert entry.prompt_preset == "Pumpkin King"
        assert (entry.edit_width, entry.edit_height) == identity_edit_output_size(80, 40)
        with PILImage.open(BytesIO(entry.source_image_bytes)) as frame:
            assert frame.format == "PNG"
            assert frame.convert("RGBA").getpixel((0, 0))[:3] == (255, 0, 0)

    _run(run())


def test_animated_avatar_uses_png_frame():
    async def run():
        caller = _user(b"frame", animated=True)
        interaction = _interaction(caller)
        cog = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(cog, interaction, costume="skeleton")
        assert caller.display_avatar.kwargs == {"size": 1024, "format": "png"}
        assert cog.entries[0].source_image_bytes == b"frame"
        assert cog.entries[0].edit_width == 1024

    _run(run())


def test_no_per_user_cooldown():
    async def run():
        cog = FakeDiffusion()
        first = _interaction(_user(b"caller-png"))
        second = _interaction(_user(b"caller-png"))
        await ImageDiffusion.halloweenify.callback(cog, first, costume="ghost")
        await ImageDiffusion.halloweenify.callback(cog, second, costume="ghost")
        assert len(cog.entries) == 2
        assert cog.entries[0].prompt == cog.entries[1].prompt

    _run(run())


def test_disallowed_channel_is_ephemeral_and_not_queued():
    async def run():
        cog = FakeDiffusion(allowed=False)
        interaction = _interaction(_user(b"caller-png"))
        await ImageDiffusion.halloweenify.callback(cog, interaction, costume="vampire")
        assert cog.entries == []
        args = interaction.response.send_message.await_args
        assert args.args[0] == "This command is not allowed in this channel!"
        assert args.kwargs["ephemeral"] is True

        missing_guild = _interaction(_user(b"caller-png"), guild_id=None)
        cog = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(cog, missing_guild, costume="vampire")
        assert cog.entries == []

    _run(run())


def test_avatar_fetch_failure_is_ephemeral():
    async def run():
        cog = FakeDiffusion()
        interaction = _interaction(_user(b"caller-png", fail=True))
        await ImageDiffusion.halloweenify.callback(cog, interaction, costume="vampire")
        assert cog.entries == []
        args = interaction.response.send_message.await_args
        assert "couldn't fetch your avatar" in args.args[0]
        assert args.kwargs["ephemeral"] is True

        empty = _interaction(_user(b""))
        cog = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(cog, empty, costume="vampire")
        assert cog.entries == []

    _run(run())


def test_unknown_costume_is_ephemeral_and_does_not_read_the_avatar():
    async def run():
        caller = _user(b"caller-png")
        interaction = _interaction(caller)
        cog = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(cog, interaction, costume="clown")
        assert cog.entries == []
        assert caller.display_avatar.kwargs is None
        args = interaction.response.send_message.await_args
        assert "Unknown costume" in args.args[0]
        assert "vampire" in args.args[0]
        assert args.kwargs["ephemeral"] is True

    _run(run())


def test_queue_full_message_comes_from_enqueue_image():
    async def run():
        ready = asyncio.Event()
        ready.set()
        queue = asyncio.Queue(maxsize=5)
        for _ in range(5):
            queue.put_nowait(object())
        cog = SimpleNamespace(
            comfy_ready=ready,
            comfy_startup_error=None,
            image_queue=queue,
            _is_allowed_channel=lambda guild_id, channel_id: True,
        )

        async def enqueue_image(entry, interaction, **kwargs):
            return await ImageDiffusion.enqueue_image(cog, entry, interaction, **kwargs)

        cog.enqueue_image = enqueue_image
        interaction = _interaction(_user(b"caller-png"))
        await ImageDiffusion.halloweenify.callback(cog, interaction, costume="vampire")
        assert queue.qsize() == 5
        args = interaction.response.send_message.await_args
        assert args.args[0] == QUEUE_FULL_MESSAGE
        assert args.args[0] == "The image queue is full right now, try again in a minute."
        assert interaction.response.send_message.await_count == 1

    _run(run())


def test_prompt_details_show_preset_and_details_and_regenerate_keeps_the_costume(monkeypatch):
    picks = []

    def choice(seq):
        picks.append(tuple(seq))
        return seq[0]

    monkeypatch.setattr("cogs.imagediffusion.random.choice", choice)

    async def run():
        caller = _user(b"caller-png")
        interaction = _interaction(caller)
        cog = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(
            cog, interaction, costume="random", details="  sparkles\nand bats  "
        )
        assert picks == [tuple(HALLOWEENIFY_PRESET_ORDER)]
        entry = cog.entries[0]
        assert entry.prompt_preset == halloweenify_preset_label(HALLOWEENIFY_PRESET_ORDER[0])
        assert entry.prompt_details == "sparkles and bats"
        assert "(Random)" not in entry.caption_label

        view = PromptDetailButton(entry.prompt, "", entry.prompt, "", entry, 7, 1.5, 10)
        assert view.retain_source is True
        assert view.prompt_preset == entry.prompt_preset
        assert view.prompt_details == "sparkles and bats"
        details_interaction = SimpleNamespace(
            user=SimpleNamespace(id=8),
            response=SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False),
        )
        button = next(child for child in view.children if getattr(child, "label", None) == "See prompt details")
        await button.callback(details_interaction)
        embed = details_interaction.response.send_message.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Costume"] == entry.prompt_preset
        assert fields["Extra details"] == "sparkles and bats"
        assert "sparkles and bats" in fields["User Prompt"]
        assert details_interaction.response.send_message.await_args.kwargs["ephemeral"] is True

        regen = SimpleNamespace(
            user=SimpleNamespace(id=7, mention="<@7>"),
            response=SimpleNamespace(send_message=AsyncMock()),
            client=SimpleNamespace(get_cog=lambda name: cog),
        )
        regenerate = next(child for child in view.children if getattr(child, "label", None) == "Regenerate")
        await regenerate.callback(regen)
        assert len(picks) == 1
        again = cog.entries[1]
        assert again.prompt == entry.prompt
        assert again.prompt_preset == entry.prompt_preset
        assert again.prompt_details == entry.prompt_details
        assert again.caption_label == entry.caption_label
        assert again.source_image_bytes == b"caller-png"
        assert again.retain_source is True
        assert again.edit_strength == HALLOWEENIFY_EDIT_STRENGTH
        assert HALLOWEENIFY_STYLE_PRESERVATION in again.prompt
        assert generation_caption(again, "preview") == f"{entry.caption_label} — <@7>"

        view._cooldown.clear()
        view.source_image_bytes = None
        missing = SimpleNamespace(
            user=SimpleNamespace(id=7),
            response=SimpleNamespace(send_message=AsyncMock()),
            client=SimpleNamespace(get_cog=lambda name: cog),
        )
        await regenerate.callback(missing)
        message = missing.response.send_message.await_args.args[0]
        assert "/halloweenify" in message
        assert missing.response.send_message.await_args.kwargs["ephemeral"] is True

    _run(run())


def _command_tree():
    connection = SimpleNamespace(_command_tree=None)
    client = SimpleNamespace(http=SimpleNamespace(), _connection=connection)
    return discord.app_commands.CommandTree(client)


def test_halloweenify_is_kept_off_the_tree_when_disabled(monkeypatch):
    assert HALLOWEENIFY_ENABLED is True
    monkeypatch.setattr("cogs.imagediffusion.HALLOWEENIFY_ENABLED", False)
    assert ImageDiffusion.halloweenify.name == "halloweenify"
    assert [param.name for param in ImageDiffusion.halloweenify.parameters] == [
        "frame",
        "costume",
        "details",
        "image",
    ]
    kept = app_commands_for_tree([ImageDiffusion.imagegen_group, ImageDiffusion.halloweenify])
    assert [command.name for command in kept] == ["imagegen"]

    async def run():
        holder = SimpleNamespace(
            __cog_app_commands__=[
                SimpleNamespace(name="imagegen"),
                SimpleNamespace(name="halloweenify"),
            ]
        )
        await ImageDiffusion.cog_load(holder)
        assert [command.name for command in holder.__cog_app_commands__] == ["imagegen"]

    _run(run())

    tree = _command_tree()
    for command in app_commands_for_tree([ImageDiffusion.halloweenify]):
        tree.add_command(command)
    assert tree.get_command("halloweenify") is None
    assert all(getattr(command, "name", None) != "halloweenify" for command in tree.get_commands())


def test_halloweenify_registers_when_the_flag_is_on(monkeypatch):
    monkeypatch.setattr("cogs.imagediffusion.HALLOWEENIFY_ENABLED", True)
    kept = app_commands_for_tree([ImageDiffusion.imagegen_group, ImageDiffusion.halloweenify])
    assert [command.name for command in kept] == ["imagegen", "halloweenify"]

    async def run():
        holder = SimpleNamespace(
            __cog_app_commands__=[
                SimpleNamespace(name="imagegen"),
                SimpleNamespace(name="halloweenify"),
            ]
        )
        await ImageDiffusion.cog_load(holder)
        assert [command.name for command in holder.__cog_app_commands__] == ["imagegen", "halloweenify"]

    _run(run())

    tree = _command_tree()
    tree.add_command(ImageDiffusion.halloweenify)
    registered = tree.get_command("halloweenify")
    assert registered.name == "halloweenify"
    assert [param.name for param in registered.parameters] == [
        "frame",
        "costume",
        "details",
        "image",
    ]


def _snapshot_files(files) -> list[tuple[str, bytes]]:
    saved = []
    for item in files:
        item.fp.seek(0)
        saved.append((item.filename, item.fp.read()))
    return saved


def test_halloween_frames_keep_the_size_and_change_the_border():
    source = PILImage.new("RGBA", (128, 96), (10, 20, 30, 255))
    corner = source.getpixel((0, 0))
    center = source.getpixel((64, 48))
    for key in HALLOWEEN_FRAMES:
        framed = apply_halloween_frame(source, key)
        assert framed.size == source.size
        assert framed.getpixel((0, 0)) != (10, 20, 30, 255)
        assert framed.getpixel((64, 48)) == center
        assert source.getpixel((0, 0)) == corner
        assert source.getpixel((64, 48)) == center
        png = render_halloween_frame_png(_png(128, 96, (10, 20, 30)), key)
        with PILImage.open(BytesIO(png)) as decoded:
            assert decoded.size == (128, 96)
            assert decoded.convert("RGBA").getpixel((0, 0)) != (10, 20, 30, 255)
            assert decoded.convert("RGBA").getpixel((64, 48))[:3] == (10, 20, 30)
    with pytest.raises(ValueError):
        apply_halloween_frame(source, "nope")
    with pytest.raises(ValueError):
        apply_halloween_frame(source, None)


def test_halloweenify_result_files_keep_original_then_framed_result():
    source = _png(128, 96, (255, 0, 0))
    result = PILImage.new("RGBA", (128, 96), (0, 0, 255, 255))
    files = halloweenify_result_files(source, result, frame="pumpkin")
    try:
        saved = _snapshot_files(files)
    finally:
        for item in files:
            item.close()
    assert [name for name, _blob in saved] == ["original.png", "halloweenified.png"]
    with PILImage.open(BytesIO(saved[0][1])) as original:
        assert original.size == (128, 96)
        assert original.convert("RGBA").getpixel((64, 48))[:3] == (255, 0, 0)
    with PILImage.open(BytesIO(saved[1][1])) as framed:
        assert framed.size == result.size
        assert framed.convert("RGBA").getpixel((64, 48))[:3] == (0, 0, 255)
        assert framed.convert("RGBA").getpixel((0, 0))[:3] != (0, 0, 255)
    assert result.getpixel((0, 0)) == (0, 0, 255, 255)


def test_frame_only_skips_the_queue_and_sends_both_files():
    async def run():
        png = _png(128, 96, (10, 20, 30))
        caller = _user(png)
        interaction = _interaction(caller)
        cog = FakeDiffusion()
        saved = {}

        async def capture(*args, **kwargs):
            saved["content"] = kwargs.get("content")
            saved["ephemeral"] = kwargs.get("ephemeral", False)
            saved["files"] = _snapshot_files(kwargs["files"])

        interaction.response.send_message.side_effect = capture
        await ImageDiffusion.halloweenify.callback(
            cog, interaction, costume="none", frame="bats"
        )
        assert cog.entries == []
        assert caller.display_avatar.kwargs == {"size": 1024, "static_format": "png"}
        assert saved["ephemeral"] is False
        assert saved["content"] == "**Halloweenify** (Bats)"
        assert [name for name, _blob in saved["files"]] == ["original.png", "halloweenified.png"]
        with PILImage.open(BytesIO(saved["files"][1][1])) as framed:
            assert framed.size == (128, 96)
            assert framed.convert("RGBA").getpixel((0, 0))[:3] != (10, 20, 30)
            assert framed.convert("RGBA").getpixel((64, 48))[:3] == (10, 20, 30)

    _run(run())


def test_frame_only_without_a_frame_does_not_read_the_avatar():
    async def run():
        caller = _user(_png(32, 32))
        interaction = _interaction(caller)
        cog = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(cog, interaction, costume="none")
        assert cog.entries == []
        assert caller.display_avatar.kwargs is None
        args = interaction.response.send_message.await_args
        assert args.args[0] == "Pick a frame to use without a costume."
        assert args.kwargs["ephemeral"] is True

    _run(run())


def test_unknown_frame_is_ephemeral_and_does_not_read_the_avatar():
    async def run():
        caller = _user(_png(32, 32))
        interaction = _interaction(caller)
        cog = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(
            cog, interaction, costume="vampire", frame="sparkles"
        )
        assert cog.entries == []
        assert caller.display_avatar.kwargs is None
        args = interaction.response.send_message.await_args
        assert "Unknown frame" in args.args[0]
        assert args.kwargs["ephemeral"] is True

    _run(run())


def test_costume_with_frame_queues_and_keeps_the_queued_message():
    async def run():
        caller = _user(b"caller-png")
        interaction = _interaction(caller)
        cog = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(
            cog, interaction, costume="vampire", frame="pumpkin"
        )
        entry = cog.entries[0]
        assert entry.halloween_frame == "pumpkin"
        assert entry.retain_source is True
        assert entry.prompt_preset == "Vampire"
        assert cog.kwargs["queued_message"] == "Queued a Halloween version of your avatar (1024×1024)."

        plain = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(
            plain, _interaction(_user(b"caller-png")), costume="vampire"
        )
        assert plain.entries[0].halloween_frame == ""
        assert plain.kwargs["queued_message"] == "Queued a Halloween version of your avatar (1024×1024)."

        view = PromptDetailButton(entry.prompt, "", entry.prompt, "", entry, 7, 1.5, 10)
        assert view.halloween_frame == "pumpkin"
        regen = SimpleNamespace(
            user=SimpleNamespace(id=7, mention="<@7>"),
            response=SimpleNamespace(send_message=AsyncMock()),
            client=SimpleNamespace(get_cog=lambda name: cog),
        )
        regenerate = next(child for child in view.children if getattr(child, "label", None) == "Regenerate")
        await regenerate.callback(regen)
        assert cog.entries[1].halloween_frame == "pumpkin"
        assert cog.entries[1].prompt == entry.prompt
        assert cog.entries[1].edit_strength == HALLOWEENIFY_EDIT_STRENGTH
        assert HALLOWEENIFY_STYLE_PRESERVATION in cog.entries[1].prompt

    _run(run())


def _identity_edit_workflow() -> dict:
    return {
        "53": {
            "class_type": "KSampler",
            "inputs": {"seed": 0, "steps": 10, "cfg": 1, "denoise": 1},
        },
        "79": {
            "class_type": "Krea2EditModelPatch",
            "inputs": {"ref_boost": 4.0, "ref_boost_a": 1.0, "fit_mode": "fit"},
        },
        "82": {
            "class_type": "EmptySD3LatentImage",
            "inputs": {"width": 1024, "height": 1024, "batch_size": 1},
        },
        "84": {
            "class_type": "Krea2EditGroundedEncode",
            "inputs": {"prompt": "", "grounding_px": 768},
        },
        "85": {"class_type": "Krea2EditGroundedEncode", "inputs": {"prompt": ""}},
    }


@pytest.mark.parametrize("preset", [*HALLOWEENIFY_PRESET_ORDER, "random"])
def test_preservation_instruction_and_strength_for_every_costume(preset):
    """Every costume, including random, is an identity edit of the original."""
    resolved, prompt = build_halloweenify_prompt(preset, details="tiny hat", rng=random.Random(7))
    assert resolved in HALLOWEENIFY_PRESET_ORDER
    assert prompt.startswith(HALLOWEENIFY_BASE_PROMPT)
    assert HALLOWEENIFY_STYLE_PRESERVATION in prompt
    assert prompt.endswith(HALLOWEENIFY_STYLE_PRESERVATION)
    assert f"Costume: {HALLOWEENIFY_COSTUMES[resolved]}" in prompt
    for phrase in (
        "anime stays anime",
        "cartoon stays cartoon",
        "pixel art stays pixel art",
        "photo stays a photo",
        "character identity",
        "face",
        "pose",
        "composition",
        "framing",
        "color palette",
        "Do not generate a new picture",
    ):
        assert phrase in prompt

    interaction = SimpleNamespace(user=SimpleNamespace(mention="<@7>", id=7))
    image = build_halloweenify_image(
        b"avatar-bytes",
        interaction,
        preset,
        "tiny hat",
        rng=random.Random(7),
    )
    assert image.prompt == prompt
    assert image.model is Models.KREA2_IDENTITY_EDIT
    assert image.edit_strength == HALLOWEENIFY_EDIT_STRENGTH
    assert image.retain_source is True
    assert HALLOWEENIFY_EDIT_STRENGTH > 4.0

    workflow = _identity_edit_workflow()
    patched = ComfyKrea2IdentityEditModel()._apply_prompts_to_workflow(
        workflow, image.prompt, "", image
    )
    assert patched["84"]["inputs"]["prompt"] == image.prompt
    assert HALLOWEENIFY_STYLE_PRESERVATION in patched["84"]["inputs"]["prompt"]
    assert patched["79"]["inputs"]["ref_boost"] == HALLOWEENIFY_EDIT_STRENGTH
    # Turbo guidance stays at 1. Denoise stays 1 because the sampler fills an
    # empty latent; ref_boost is the dial that keeps the original.
    assert patched["53"]["inputs"]["cfg"] == 1
    assert patched["53"]["inputs"]["denoise"] == 1
    assert workflow["79"]["inputs"]["ref_boost"] == 4.0


def test_plain_identity_edit_keeps_the_workflow_strength():
    image = ImageCreation(
        "paint the jacket red",
        Models.KREA2_IDENTITY_EDIT,
        edit_width=1024,
        edit_height=1024,
    )
    assert image.edit_strength is None
    patched = ComfyKrea2IdentityEditModel()._apply_prompts_to_workflow(
        _identity_edit_workflow(), image.prompt, "", image
    )
    assert patched["79"]["inputs"]["ref_boost"] == 4.0
    assert patched["53"]["inputs"]["denoise"] == 1


_OMIT = object()


def test_frame_only_is_the_default_and_first_choice_and_skips_ai(monkeypatch):
    command = ImageDiffusion.halloweenify
    params = list(command.parameters)
    assert params[0].name == "frame"
    costume = next(param for param in params if param.name == "costume")
    assert costume.default == "none"
    assert costume.choices[0].value == "none"
    assert costume.choices[0].name == "None (frame only)"
    assert HALLOWEENIFY_ENABLED is True

    def fail_ai(*args, **kwargs):
        raise AssertionError("frame-only must not call the AI backend")

    monkeypatch.setattr("cogs.imagediffusion.build_halloweenify_image", fail_ai)
    monkeypatch.setattr("cogs.imagediffusion.enqueue_identity_edit", fail_ai)

    async def framed(costume):
        png = _png(64, 64, (10, 20, 30))
        caller = _user(png)
        interaction = _interaction(caller)
        cog = FakeDiffusion()
        saved = {}

        async def capture(*args, **kwargs):
            saved["files"] = _snapshot_files(kwargs["files"])
            saved["content"] = kwargs.get("content")

        interaction.response.send_message.side_effect = capture
        kwargs = {"frame": "candy_corn"}
        if costume is not _OMIT:
            kwargs["costume"] = costume
        await ImageDiffusion.halloweenify.callback(cog, interaction, **kwargs)
        assert cog.entries == []
        assert saved["content"] == "**Halloweenify** (Candy corn)"
        assert [name for name, _blob in saved["files"]] == ["original.png", "halloweenified.png"]
        with PILImage.open(BytesIO(saved["files"][0][1])) as original:
            assert original.size == (64, 64)
            assert original.convert("RGBA").getpixel((32, 32))[:3] == (10, 20, 30)
        with PILImage.open(BytesIO(saved["files"][1][1])) as result:
            assert result.size == (64, 64)
            assert result.convert("RGBA").getpixel((32, 32))[:3] == (10, 20, 30)
            assert result.convert("RGBA").getpixel((0, 0))[:3] != (10, 20, 30)

    for costume in (_OMIT, None, "none"):
        _run(framed(costume))

    async def bare():
        caller = _user(_png(32, 32))
        interaction = _interaction(caller)
        cog = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(cog, interaction)
        assert cog.entries == []
        assert caller.display_avatar.kwargs is None
        args = interaction.response.send_message.await_args
        assert args.args[0] == "Pick a frame to use without a costume."
        assert args.kwargs["ephemeral"] is True

    _run(bare())
