from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Optional

import discord
import wavelink
from discord.ext import commands, tasks
from discord.ext.commands import Cog

from cogs.utils.lavalink_server import LavalinkServerManager, load_lavalink_settings
from cogs.utils.utils import load_credentials

log = logging.getLogger(__name__)

ONE_MEMBER = 1
DEFAULT_VOLUME = 100  # Wavelink/Lavalink scale: 100 = 100%, max 1000
TRACK_END_GRACE_SECONDS = 20


def voice_client_is_stale(
    *,
    claimed_connected: bool,
    player_channel_id: int | None,
    actual_channel_id: int | None,
) -> bool:
    """A voice link that finished connecting but no longer matches Discord.

    A client that has not finished its handshake (claimed_connected False) is not
    stale; the gateway reconnect case leaves claimed_connected True while the
    bot is absent from every channel.
    """
    if not claimed_connected:
        return False
    if player_channel_id is None or actual_channel_id is None:
        return True
    return player_channel_id != actual_channel_id


class ScooperPlayer(wavelink.Player):
    """Wavelink player that joins unmuted and survives either voice-event order.

    Discord does not guarantee VOICE_STATE_UPDATE before VOICE_SERVER_UPDATE.
    Stock Wavelink only forwards the voice credentials when the server update
    arrives, so a server-update-first handshake never reaches Lavalink and
    connect() times out. Dispatch again from the state update once both halves
    exist.
    """

    async def on_voice_state_update(self, data, /) -> None:
        await super().on_voice_state_update(data)
        if data.get("channel_id"):
            await self._dispatch_voice_update()

    async def connect(
        self,
        *,
        timeout: float = 10.0,
        reconnect: bool,
        self_deaf: bool = False,
        self_mute: bool = False,
    ) -> None:
        # Wait for this attempt's voice update. A reused player can still have
        # the previous connection event set, which makes connect() return
        # before Lavalink has the new token.
        self._connection_event.clear()
        await super().connect(
            timeout=timeout,
            reconnect=reconnect,
            self_deaf=self_deaf,
            self_mute=self_mute,
        )

ASSETS_DIR = Path(__file__).resolve().parent / "assets" / "music"
SPINNING_DISC_FILE = "spinning_disc.gif"
STATIC_DISC_FILE = "vinyl_record.png"
PLAYING_ACCENT = discord.Colour(0x1DB954)
PAUSED_ACCENT = discord.Colour(0xE8A838)
ENDED_ACCENT = discord.Colour(0x4E5058)
NO_MENTIONS = discord.AllowedMentions.none()


def _plain(text: str | None) -> str:
    if not text:
        return ""
    cleaned = " ".join(str(text).split())
    return discord.utils.escape_mentions(discord.utils.escape_markdown(cleaned))


def format_track_length(length_ms: int | None) -> str | None:
    if not length_ms or length_ms <= 0:
        return None
    total_seconds = int(length_ms) // 1000
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def linked_track_title(title: str | None, uri: str | None) -> str:
    label = _plain(title or "Unknown track").replace("[", "\\[").replace("]", "\\]")
    if not uri or not uri.startswith(("http://", "https://")):
        return label
    if any(char in uri for char in " \n()<>"):
        return label
    return f"[{label}]({uri})"


def track_subtitle(author: str | None, length_ms: int | None) -> str | None:
    parts: list[str] = []
    if author:
        parts.append(_plain(author))
    length = format_track_length(length_ms)
    if length:
        parts.append(length)
    if not parts:
        return None
    return " · ".join(parts)


def requester_label(entry: "MusicEntry") -> str:
    author = entry.ctx.message.author
    name = getattr(author, "display_name", None) or getattr(author, "name", None) or "someone"
    return _plain(str(name)) or "someone"


def mode_line(*, repeat: bool, loop: bool, shuffle: bool) -> str | None:
    parts = []
    if repeat:
        parts.append("🔂 Repeat")
    if loop:
        parts.append("🔁 Loop")
    if shuffle:
        parts.append("🔀 Shuffle")
    if not parts:
        return None
    return " · ".join(parts)


def message_is_latest(*, candidate_id: int | None, newest_id: int | None) -> bool:
    if candidate_id is None or newest_id is None:
        return False
    return candidate_id == newest_id


def should_reuse_now_playing(*, same_channel: bool, is_latest: bool) -> bool:
    """Edit the player only when that card is still the newest message in its channel."""
    return same_channel and is_latest


def playback_will_continue(
    *,
    stopping: bool,
    repeat_enabled: bool,
    has_repeated_entry: bool,
    queue_empty: bool,
) -> bool:
    """True when another track will start without waiting for a new !play."""
    if stopping:
        return False
    if repeat_enabled and has_repeated_entry:
        return True
    return not queue_empty


class MusicEntry:
    def __init__(self, track: wavelink.Playable, ctx: commands.Context):
        self.track = track
        self.ctx = ctx
        self.started_at: datetime | None = None


class NowPlayingView(discord.ui.LayoutView):
    """Player card. Buttons live in the container, and the disc is a title thumbnail.

    A classic embed cannot contain buttons, so the card is a components-v2
    container. The spinning disc is the section thumbnail beside the title,
    which is the small size Discord renders there.
    """

    def __init__(
        self,
        cog: "Music",
        entry: MusicEntry,
        message: discord.Message = None,
        *,
        is_paused: bool = False,
        ended: bool = False,
        controls: bool = True,
        timeout: float = 3600,
    ):
        interactive = controls and not ended
        super().__init__(timeout=timeout if interactive else None)
        self.cog = cog
        self.entry = entry
        self.message = message
        self.add_item(self._build_container(is_paused=is_paused, ended=ended, controls=interactive))

    def _build_container(self, *, is_paused: bool, ended: bool, controls: bool) -> discord.ui.Container:
        track = self.entry.track
        spinning = not is_paused and not ended
        if ended:
            accent = ENDED_ACCENT
            eyebrow = "-# PLAYBACK ENDED"
        elif is_paused:
            accent = PAUSED_ACCENT
            eyebrow = "-# PAUSED"
        else:
            accent = PLAYING_ACCENT
            eyebrow = "-# NOW PLAYING"

        header = [eyebrow, f"### {linked_track_title(track.title, getattr(track, 'uri', None))}"]
        subtitle = track_subtitle(getattr(track, "author", None), getattr(track, "length", None))
        if subtitle:
            header.append(subtitle)

        disc_url = Music.disc_attachment_url(is_playing=spinning)
        container = discord.ui.Container(accent_colour=accent)
        container.add_item(
            discord.ui.Section(
                *header,
                accessory=discord.ui.Thumbnail(
                    disc_url,
                    description="Spinning disc" if spinning else "Vinyl",
                ),
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(self._details_block(is_paused=is_paused, ended=ended))
        if controls:
            container.add_item(discord.ui.Separator())
            container.add_item(self._button_row(self._transport_buttons()))
            container.add_item(self._button_row(self._mode_buttons()))
        return container

    def _details_block(self, *, is_paused: bool, ended: bool) -> discord.ui.Item:
        name = requester_label(self.entry)
        if ended:
            text = f"Requested by **{name}**\n-# Queue another track to keep it going"
        else:
            status = "⏸️ **Paused**" if is_paused else "▶️ **Playing**"
            waiting = self.cog.music_queue.qsize()
            queue = "Queue clear" if waiting == 0 else f"**{waiting}** in queue"
            lines = [f"{status} · {queue}", f"Requested by **{name}**"]
            modes = mode_line(
                repeat=self.cog.repeat_enabled,
                loop=self.cog.loop_enabled,
                shuffle=self.cog.shuffle_mode,
            )
            if modes:
                lines.append(modes)
            started = getattr(self.entry, "started_at", None)
            if started is not None:
                lines.append(f"-# <t:{int(started.timestamp())}:R>")
            text = "\n".join(lines)

        artwork = getattr(self.entry.track, "artwork", None)
        if isinstance(artwork, str) and artwork.startswith(("http://", "https://")):
            alt = (getattr(self.entry.track, "title", None) or "Album art")[:256]
            return discord.ui.Section(
                text,
                accessory=discord.ui.Thumbnail(artwork, description=str(alt)),
            )
        return discord.ui.TextDisplay(text)

    def _button_row(self, buttons: list[discord.ui.Button]) -> discord.ui.ActionRow:
        row = discord.ui.ActionRow()
        for button in buttons:
            row.add_item(button)
        return row

    def _control(
        self,
        *,
        custom_id: str,
        emoji: str,
        label: str,
        style: discord.ButtonStyle,
        callback,
    ) -> discord.ui.Button:
        button = discord.ui.Button(custom_id=custom_id, emoji=emoji, label=label, style=style)
        button.callback = callback
        return button

    def _toggle_style(self, enabled: bool) -> discord.ButtonStyle:
        if enabled:
            return discord.ButtonStyle.primary
        return discord.ButtonStyle.secondary

    def _transport_buttons(self) -> list[discord.ui.Button]:
        return [
            self._control(custom_id="np_skip", emoji="⏭️", label="Skip", style=discord.ButtonStyle.secondary, callback=self.skip_button),
            self._control(custom_id="np_stop", emoji="⏹️", label="Stop", style=discord.ButtonStyle.danger, callback=self.stop_button),
            self._control(custom_id="np_pause", emoji="⏸️", label="Pause", style=discord.ButtonStyle.secondary, callback=self.pause_button),
            self._control(custom_id="np_resume", emoji="▶️", label="Resume", style=discord.ButtonStyle.success, callback=self.resume_button),
        ]

    def _mode_buttons(self) -> list[discord.ui.Button]:
        return [
            self._control(
                custom_id="np_repeat",
                emoji="🔂",
                label="Repeat",
                style=self._toggle_style(self.cog.repeat_enabled),
                callback=self.repeat_button,
            ),
            self._control(
                custom_id="np_loop",
                emoji="🔁",
                label="Loop",
                style=self._toggle_style(self.cog.loop_enabled),
                callback=self.loop_button,
            ),
            self._control(
                custom_id="np_shuffle",
                emoji="🔀",
                label="Shuffle",
                style=self._toggle_style(self.cog.shuffle_mode),
                callback=self.shuffle_button,
            ),
        ]

    def _check_voice(self, interaction: discord.Interaction) -> bool:
        return self.cog._same_voice_check(interaction)

    def _is_current_np(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or self.message is None:
            return False
        current = self.cog.current_np_message.get(interaction.guild.id)
        return current is not None and current.id == self.message.id

    async def _reject(self, interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            return
        await interaction.response.send_message(message, ephemeral=True)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if not self._check_voice(interaction):
            await self._reject(interaction, "You must be in the same voice channel to control playback.")
            return False
        if not self._is_current_np(interaction):
            await self._reject(interaction, "This player is no longer active.")
            return False
        return True

    async def _refresh_card(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not self.message or not self._is_current_np(interaction):
            return
        player = interaction.guild.voice_client
        is_paused = bool(player.paused) if isinstance(player, wavelink.Player) else False
        view = NowPlayingView(self.cog, self.entry, self.message, is_paused=is_paused)
        self.cog._release_np_view(interaction.guild.id)
        try:
            edited = await self.message.edit(
                view=view,
                attachments=self.cog.disc_files(is_playing=not is_paused),
                allowed_mentions=NO_MENTIONS,
            )
        except (discord.NotFound, discord.HTTPException):
            return
        self.cog._remember_np(interaction.guild.id, edited, self.entry, view)

    async def on_timeout(self) -> None:
        message = self.message
        guild = message.guild if message is not None else None
        if guild is None or self.cog.current_np_view.get(guild.id) is not self:
            return
        self.cog.current_np_view.pop(guild.id, None)
        player = guild.voice_client
        is_paused = isinstance(player, wavelink.Player) and bool(player.paused)
        frozen = NowPlayingView(self.cog, self.entry, message, is_paused=is_paused, controls=False)
        try:
            await message.edit(
                view=frozen,
                attachments=self.cog.disc_files(is_playing=not is_paused),
                allowed_mentions=NO_MENTIONS,
            )
        except discord.NotFound:
            self.cog.current_np_message.pop(guild.id, None)
            self.cog.current_np_entry.pop(guild.id, None)
        except discord.HTTPException:
            pass

    async def skip_button(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        await self.cog._button_skip(interaction)

    async def stop_button(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        await self.cog._button_stop(interaction)

    async def pause_button(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        await self.cog._button_pause(interaction)
        await self._refresh_card(interaction)

    async def resume_button(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        await self.cog._button_resume(interaction)
        await self._refresh_card(interaction)

    async def repeat_button(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        await self.cog._button_repeat(interaction)
        await self._refresh_card(interaction)

    async def loop_button(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        await self.cog._button_loop(interaction)
        await self._refresh_card(interaction)

    async def shuffle_button(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        await self.cog._button_shuffle(interaction)
        await self._refresh_card(interaction)


class Music(Cog):
    """Commands for playing music in voice chat."""

    def __init__(self, bot):
        self.bot = bot
        self._verify_disc_assets()
        self.music_queue = asyncio.Queue()
        self.next_song = asyncio.Event()
        self.music_player.start()
        self.idle_timeout.start()

        self.repeat_enabled = False
        self.repeated_entry = None
        self.loop_enabled = False
        self.shuffle_mode = False
        self.current_np_message = {}
        self.current_np_entry = {}
        self.current_np_view = {}
        self._stopping = False
        self._lavalink_bootstrap_task: asyncio.Task | None = None
        self._lavalink_shutdown_done = False

    @staticmethod
    def _verify_disc_assets() -> None:
        for name in (SPINNING_DISC_FILE, STATIC_DISC_FILE):
            path = ASSETS_DIR / name
            if not path.is_file():
                raise FileNotFoundError(
                    f"Missing music asset {path}. Run agent-tools/generate_disc_assets.py once."
                )

    @staticmethod
    def disc_asset_name(*, is_playing: bool) -> str:
        return SPINNING_DISC_FILE if is_playing else STATIC_DISC_FILE

    @classmethod
    def disc_attachment_url(cls, *, is_playing: bool) -> str:
        return f"attachment://{cls.disc_asset_name(is_playing=is_playing)}"

    @classmethod
    def disc_files(cls, *, is_playing: bool) -> list[discord.File]:
        name = cls.disc_asset_name(is_playing=is_playing)
        return [discord.File(ASSETS_DIR / name, filename=name)]

    async def reset_player_controls(self):
        self.repeat_enabled = False
        self.repeated_entry = None
        self.loop_enabled = False
        self.shuffle_mode = False

    def _lavalink_ready(self) -> bool:
        return bool(wavelink.Pool.nodes)

    def _get_player(self, guild: discord.Guild) -> wavelink.Player | None:
        vc = guild.voice_client
        if isinstance(vc, wavelink.Player):
            return vc
        return None

    @staticmethod
    def _actual_channel_id(guild: discord.Guild) -> int | None:
        me = guild.me
        if me is None or me.voice is None or me.voice.channel is None:
            return None
        return me.voice.channel.id

    def _player_is_stale(self, guild: discord.Guild, player: wavelink.Player) -> bool:
        channel = player.channel
        return voice_client_is_stale(
            claimed_connected=bool(player.connected),
            player_channel_id=getattr(channel, "id", None),
            actual_channel_id=self._actual_channel_id(guild),
        )

    def _needs_reconnect(self, guild: discord.Guild, player: wavelink.Player) -> bool:
        """Replace a client that will not produce audio.

        A handshake that Discord has already accepted (the bot is in a channel)
        is left alone even if wavelink has not flipped ``connected`` yet.
        """
        if self._player_is_stale(guild, player):
            return True
        if not player.connected and self._actual_channel_id(guild) is None:
            return True
        return False

    async def _drop_voice_client(self, guild: discord.Guild) -> None:
        """Remove a voice client Discord is no longer honoring.

        A gateway reconnect does not clear wavelink's player. channel.connect
        then raises "already connected" or, worse, !play sees the old client
        and never sends a new voice token to Lavalink.
        """
        vc = guild.voice_client
        if vc is None:
            return
        try:
            await vc.disconnect(force=True)
        except TypeError:
            try:
                await vc.disconnect()
            except Exception:
                log.exception("Failed to disconnect voice client in guild %s", guild.id)
        except Exception:
            log.exception("Failed to disconnect voice client in guild %s", guild.id)
        leftover = guild.voice_client
        if leftover is not None:
            try:
                leftover.cleanup()
            except Exception:
                log.exception("Failed to cleanup voice client in guild %s", guild.id)

    async def _connect_to_author(self, ctx: commands.Context) -> wavelink.Player | None:
        """Join the command author's voice channel if not already connected."""
        player = self._get_player(ctx.guild)
        if player and not self._needs_reconnect(ctx.guild, player):
            return player
        if player or ctx.guild.voice_client is not None:
            log.warning(
                "Replacing stale voice client in %s before joining",
                ctx.guild.name,
            )
            await self._drop_voice_client(ctx.guild)

        if not ctx.author.voice or not ctx.author.voice.channel:
            return None

        channel = ctx.author.voice.channel
        try:
            # self_deaf avoids receiving audio. self_mute must stay False or
            # Discord will not transmit the Lavalink audio.
            player = await channel.connect(
                cls=ScooperPlayer,
                self_deaf=True,
                self_mute=False,
            )
            log.info("Joined voice channel %s in %s", channel.name, ctx.guild.name)
            return player
        except Exception as e:
            log.error("Failed to join voice channel %s: %s", channel.name, e)
            await self._drop_voice_client(ctx.guild)
            return None

    async def _ensure_connected_for_entry(self, entry: MusicEntry) -> bool:
        """Ensure the bot is in a voice channel before playing a queued track."""
        ctx = entry.ctx
        player = self._get_player(ctx.guild)
        if player and self._needs_reconnect(ctx.guild, player):
            log.warning(
                "Voice client in %s is not actually connected; reconnecting for %r",
                ctx.guild.name,
                entry.track.title,
            )
            await self._drop_voice_client(ctx.guild)
            player = None

        if player:
            author_channel = ctx.author.voice.channel if ctx.author.voice else None
            if author_channel and player.channel != author_channel:
                await player.move_to(author_channel, self_deaf=True, self_mute=False)
            if self._actual_channel_id(ctx.guild) is not None:
                return True
            log.warning(
                "Player for %s still has no Discord voice state after move",
                ctx.guild.name,
            )
            await self._drop_voice_client(ctx.guild)

        player = await self._connect_to_author(ctx)
        if player and self._actual_channel_id(ctx.guild) is not None:
            return True

        log.warning(
            "Skipping queued track %r; could not join a voice channel (requester left VC?)",
            entry.track.title,
        )
        self._signal_next_song()
        return False

    async def _wait_for_track_end(self, entry: MusicEntry, player: wavelink.Player) -> None:
        """Wait until Lavalink finishes the track, but do not wedge the queue.

        A track sent with no live voice link never emits track end. Without a
        bound, every later !play sits behind that wait.
        """
        length_ms = entry.track.length or 0
        if length_ms <= 0:
            await self.next_song.wait()
            return
        timeout = length_ms / 1000 + TRACK_END_GRACE_SECONDS
        try:
            await asyncio.wait_for(self.next_song.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning(
                "No track-end event for %r after %.0fs; advancing the queue",
                entry.track.title,
                timeout,
            )
            try:
                await player.stop()
            except Exception:
                log.exception("Failed to stop %r after a missing track-end event", entry.track.title)

    def _playback_will_continue(self) -> bool:
        return playback_will_continue(
            stopping=self._stopping,
            repeat_enabled=self.repeat_enabled,
            has_repeated_entry=self.repeated_entry is not None,
            queue_empty=self.music_queue.empty(),
        )

    def _release_np_view(self, guild_id: int) -> None:
        view = self.current_np_view.pop(guild_id, None)
        if view is not None and not view.is_finished():
            view.stop()

    def _remember_np(
        self,
        guild_id: int,
        message: discord.Message,
        entry: MusicEntry,
        view: NowPlayingView,
    ) -> None:
        view.message = message
        self.current_np_message[guild_id] = message
        self.current_np_entry[guild_id] = entry
        self.current_np_view[guild_id] = view

    def _forget_np(self, guild_id: int) -> None:
        self._release_np_view(guild_id)
        self.current_np_message.pop(guild_id, None)
        self.current_np_entry.pop(guild_id, None)

    async def _newest_message_id(self, channel) -> int | None:
        try:
            async for recent in channel.history(limit=1):
                return recent.id
        except (discord.Forbidden, discord.HTTPException, AttributeError) as exc:
            log.warning("Could not read channel history while updating now playing: %s", exc)
            return None
        return None

    async def _message_is_latest(self, message: discord.Message) -> bool:
        newest_id = await self._newest_message_id(message.channel)
        return message_is_latest(candidate_id=message.id, newest_id=newest_id)

    async def invalidate_current_np(self, guild_id: int, entry: MusicEntry = None):
        """Turn the live card into a finished one, but keep it so the next track can reuse it."""
        message = self.current_np_message.get(guild_id)
        if message is None:
            return
        shown = entry or self.current_np_entry.get(guild_id)
        self._release_np_view(guild_id)
        if shown is None:
            return
        ended = NowPlayingView(self, shown, message, ended=True)
        try:
            await message.edit(
                view=ended,
                attachments=self.disc_files(is_playing=False),
                allowed_mentions=NO_MENTIONS,
            )
        except discord.NotFound:
            self._forget_np(guild_id)
        except discord.HTTPException:
            log.warning("Could not mark the now playing card ended in guild %s", guild_id)

    async def present_now_playing(self, entry: MusicEntry, *, is_paused: bool = False) -> discord.Message | None:
        """Edit the current card when it is still the newest message. Otherwise post a new one."""
        guild_id = entry.ctx.guild.id
        entry.started_at = discord.utils.utcnow()
        view = NowPlayingView(self, entry, is_paused=is_paused)
        previous = self.current_np_message.get(guild_id)
        same_channel = False
        if previous is not None:
            previous_channel = getattr(previous, "channel", None)
            ctx_channel = getattr(entry.ctx, "channel", None)
            same_channel = (
                previous_channel is not None
                and ctx_channel is not None
                and previous_channel.id == ctx_channel.id
            )
        is_latest = False
        if same_channel and previous is not None:
            is_latest = await self._message_is_latest(previous)
        reuse = should_reuse_now_playing(same_channel=same_channel, is_latest=is_latest)

        if reuse and previous is not None:
            self._release_np_view(guild_id)
            try:
                edited = await previous.edit(
                    view=view,
                    attachments=self.disc_files(is_playing=not is_paused),
                    allowed_mentions=NO_MENTIONS,
                )
            except discord.NotFound:
                self._forget_np(guild_id)
            except discord.HTTPException:
                log.warning("Could not edit the now playing card for %r", entry.track.title)
            else:
                self._remember_np(guild_id, edited, entry, view)
                log.info("Edited now playing card for %r", entry.track.title)
                return edited

        await self.invalidate_current_np(guild_id)
        try:
            message = await entry.ctx.send(
                view=view,
                files=self.disc_files(is_playing=not is_paused),
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException:
            log.exception("Could not post a now playing card for %r", entry.track.title)
            return None
        self._remember_np(guild_id, message, entry, view)
        log.info("Posted a new now playing card for %r", entry.track.title)
        return message

    async def get_entry(self):
        if self.repeat_enabled:
            entry = self.repeated_entry if self.repeated_entry else await self.music_queue.get()
            if not self.repeated_entry:
                self.repeated_entry = entry
            return entry
        if self.shuffle_mode:
            items = []
            while True:
                try:
                    items.append(self.music_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if not items:
                items.append(await self.music_queue.get())
            entry = random.choice(items)
            items.remove(entry)
            for e in items:
                await self.music_queue.put(e)
            return entry
        return await self.music_queue.get()

    def _signal_next_song(self):
        self.bot.loop.call_soon_threadsafe(self.next_song.set)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        if self._stopping:
            return
        player = payload.player
        if not player or not player.guild:
            return
        self._signal_next_song()

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload):
        log.error("Track exception: %s", payload.exception)
        self._signal_next_song()

    @tasks.loop(seconds=1)
    async def music_player(self):
        try:
            await self._run_music_player()
        except Exception:
            log.exception("Unhandled error in music_player; task will continue")

    async def _run_music_player(self):
        self.next_song.clear()
        if self.music_queue.empty() and not self.repeat_enabled:
            connected = [vc for vc in self.bot.voice_clients if vc.channel]
            if connected:
                log.info(
                    "Music queue empty; waiting for next !play (%s voice channel(s) connected)",
                    len(connected),
                )
        entry = await self.get_entry()

        if self.loop_enabled and not self.repeat_enabled:
            await self.music_queue.put(entry)

        if not await self._ensure_connected_for_entry(entry):
            if not self._playback_will_continue():
                await self.invalidate_current_np(entry.ctx.guild.id)
            return

        if await self.bot_is_alone(entry.ctx):
            if not self._playback_will_continue():
                await self.invalidate_current_np(entry.ctx.guild.id)
            return

        player = self._get_player(entry.ctx.guild)
        if not player:
            log.error("No Wavelink player for guild %s", entry.ctx.guild.id)
            self._signal_next_song()
            if not self._playback_will_continue():
                await self.invalidate_current_np(entry.ctx.guild.id)
            return

        try:
            await self.present_now_playing(entry)
            await player.play(entry.track, volume=DEFAULT_VOLUME)
            if self.repeat_enabled:
                self.repeated_entry = entry
        except Exception as e:
            log.error("Unexpected error while playing %s: %s", entry.track.uri, e)
            await self.error_playing_embed(entry)
            self.repeated_entry = None
            self._signal_next_song()
            if not self._playback_will_continue():
                await self.invalidate_current_np(entry.ctx.guild.id, entry=entry)
            return

        await self._wait_for_track_end(entry, player)
        if not self._playback_will_continue():
            await self.invalidate_current_np(entry.ctx.guild.id, entry=entry)

    @tasks.loop(seconds=30)
    async def idle_timeout(self):
        for voice_client in list(self.bot.voice_clients):
            channel = getattr(voice_client, "channel", None)
            guild = getattr(voice_client, "guild", None)
            if channel is None or guild is None:
                continue
            if isinstance(voice_client, wavelink.Player) and self._player_is_stale(guild, voice_client):
                log.warning(
                    "Dropping stale voice client in %s (Discord no longer has the bot in %s)",
                    guild.name,
                    getattr(channel, "name", channel.id),
                )
                await self._drop_voice_client(guild)
                await self.invalidate_current_np(guild.id)
                continue
            if len(channel.voice_states) <= ONE_MEMBER:
                guild_id = guild.id
                await voice_client.disconnect()
                await self.invalidate_current_np(guild_id)

    @idle_timeout.before_loop
    async def before_timeout(self):
        await self.bot.wait_until_ready()

    async def bot_is_alone(self, ctx):
        vc = ctx.voice_client
        if vc is None or vc.channel is None:
            return False

        number_of_members = len(vc.channel.voice_states)
        if number_of_members <= ONE_MEMBER:
            while not self.music_queue.empty():
                self.music_queue.get_nowait()
            embed = discord.Embed(
                title='Disconnecting to save my owner some bandwidth',
                description='{} other(s) detected as connected to this channel'.format(number_of_members - 1),
                colour=discord.Colour.blue(),
            )
            await ctx.send(embed=embed)
            return True
        return False

    async def error_playing_embed(self, entry: MusicEntry):
        embed = discord.Embed(
            title='Error While Playing:',
            description=entry.track.uri or str(entry.track),
            colour=discord.Colour.blue(),
        )
        await entry.ctx.send(embed=embed)

    def _same_voice_check(self, interaction):
        from cogs.utils.server_config import get_guild_config

        config = get_guild_config(interaction.guild.id)
        if config and config.music_dj_role_id:
            if any(role.id == config.music_dj_role_id for role in interaction.user.roles):
                return True

        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.channel:
            return False
        if not interaction.user.voice or not interaction.user.voice.channel:
            return False
        return interaction.user.voice.channel == voice_client.channel

    async def _button_skip(self, interaction):
        if self.repeat_enabled:
            await interaction.response.send_message("Can't skip while Repeat is enabled!", ephemeral=True)
            return
        await interaction.response.defer()
        player = self._get_player(interaction.guild)
        if player and player.playing:
            await player.skip(force=True)

    async def _button_stop(self, interaction):
        await interaction.response.defer()
        self._stopping = True
        while not self.music_queue.empty():
            self.music_queue.get_nowait()
        player = self._get_player(interaction.guild)
        if player and (player.playing or player.paused):
            player.queue.clear()
            await player.disconnect()
        guild_id = interaction.guild.id
        await self.invalidate_current_np(guild_id)
        self._signal_next_song()
        self._stopping = False

    async def _button_pause(self, interaction):
        player = self._get_player(interaction.guild)
        if player and player.playing:
            await player.pause(True)
            await interaction.response.defer()
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    async def _button_resume(self, interaction):
        player = self._get_player(interaction.guild)
        if player and player.paused:
            await player.pause(False)
            await interaction.response.defer()
        else:
            await interaction.response.send_message("Player is not paused.", ephemeral=True)

    async def _button_repeat(self, interaction):
        self.repeat_enabled = not self.repeat_enabled
        if not self.repeat_enabled:
            self.repeated_entry = None
        await interaction.response.defer()

    async def _button_loop(self, interaction):
        self.loop_enabled = not self.loop_enabled
        await interaction.response.defer()

    async def _button_shuffle(self, interaction):
        if self.repeat_enabled:
            await interaction.response.send_message("Can't use shuffle mode while repeat is enabled!", ephemeral=True)
            return
        self.shuffle_mode = not self.shuffle_mode
        await interaction.response.defer()

    @music_player.before_loop
    async def before_music(self):
        await self.bot.wait_until_ready()

    @commands.command()
    async def join(self, ctx):
        """Joins the voice channel."""
        pass

    @commands.group(invoke_without_command=True)
    async def play(self, ctx, *, url):
        """Plays a URL or search query (YouTube, Spotify, SoundCloud, etc.)."""
        await self._play(ctx, url)

    @play.command(name="shuffle")
    async def play_shuffle(self, ctx, *, url):
        """Plays and shuffles a playlist or album before queuing."""
        await self._play(ctx, url, shuffle=True, expand_collection=True)

    @play.command(name="playlist")
    async def play_playlist(self, ctx, *, url):
        """Plays a playlist or album from YouTube or Spotify."""
        await self._play(ctx, url, expand_collection=True)

    @staticmethod
    def _is_url(query: str) -> bool:
        q = query.strip().lower()
        return q.startswith(("http://", "https://"))

    async def _search(self, query: str, source: Optional[str] = None):
        try:
            if source is None:
                result = await wavelink.Playable.search(query)
            else:
                result = await wavelink.Playable.search(query, source=source)
        except wavelink.LavalinkLoadException as e:
            log.warning("Lavalink could not load %r via %s: %s", query, source or "url", e)
            return None
        return result if result else None

    @staticmethod
    def _pick_best_track(query: str, tracks: list[wavelink.Playable]) -> wavelink.Playable | None:
        if not tracks:
            return None
        query_lower = query.lower()
        query_words = {word for word in query_lower.split() if len(word) > 2}

        def score(track: wavelink.Playable) -> int:
            title = (track.title or "").lower()
            if title in query_lower or query_lower in title:
                return 1000
            title_words = {word for word in title.split() if len(word) > 2}
            return len(query_words & title_words)

        return max(tracks, key=score)

    async def _resolve_tracks(
        self,
        query: str,
        *,
        expand_collection: bool = False,
    ) -> list[wavelink.Playable]:
        query = query.strip()
        result = None
        is_url = self._is_url(query)
        single_track = not is_url and not expand_collection

        if is_url:
            result = await self._search(query)
        else:
            # Wavelink defaults to ytmsearch, which we do not have enabled.
            # Try Spotify first (albums/playlists), then YouTube via LavaSrc ytdlp.
            for source in ("spsearch", "ytsearch"):
                result = await self._search(query, source=source)
                if result:
                    log.info("Resolved %r via %s", query, source)
                    break

        if not result:
            return []

        if isinstance(result, wavelink.Playlist):
            tracks = list(result.tracks)
            if single_track:
                best = self._pick_best_track(query, tracks)
                return [best] if best else []
            return tracks

        tracks = list(result)
        if single_track:
            best = self._pick_best_track(query, tracks)
            return [best] if best else []
        return tracks

    async def _play(self, ctx, url, shuffle=False, expand_collection=False):
        if not self._lavalink_ready():
            embed = discord.Embed(
                title='Music unavailable',
                description='Lavalink is not connected. Bot owner: `!lavalink start`',
                colour=discord.Colour.red(),
            )
            await ctx.send(embed=embed)
            return

        async with ctx.typing():
            try:
                tracks = await self._resolve_tracks(url, expand_collection=expand_collection)
            except Exception as e:
                log.warning("Failed to resolve tracks for %s: %s", url, e)
                tracks = []

            if not tracks:
                embed = discord.Embed(
                    title='Error',
                    description='Could not find any playable tracks for that query.',
                    colour=discord.Colour.red(),
                )
                await ctx.send(embed=embed)
                return

            if shuffle:
                random.shuffle(tracks)

            for track in tracks:
                entry = MusicEntry(track, ctx)
                await self.music_queue.put(entry)

            description = url if len(tracks) == 1 else '{} songs'.format(len(tracks))
            embed = discord.Embed(
                title='Queued up',
                description=description,
                colour=discord.Colour.blue(),
            )

        await ctx.send(embed=embed)

    @commands.command()
    async def shuffle(self, ctx):
        """Shuffles the current music queue."""
        if self.repeat_enabled:
            await ctx.send("Can't shuffle while repeat is enabled!")
            return

        try:
            random.shuffle(self.music_queue._queue)
        except Exception as e:
            log.warning("Failed to shuffle the music queue: {}".format(e))
            await ctx.send("Error shuffling music queue!")
            return

        await ctx.message.add_reaction('👍')

    @commands.command()
    async def volume(self, ctx, volume: int):
        """Adjust the bot's voice volume (0-100, default 100)."""
        player = self._get_player(ctx.guild)
        if not player:
            await ctx.send("Not connected to a voice channel.")
            return

        original = player.volume
        clamped = max(0, min(100, volume))
        await player.set_volume(clamped)

        description = '{}% -> {}%'.format(str(original), str(clamped))
        embed = discord.Embed(
            title='Player Volume 🔊',
            description=description,
            colour=discord.Colour.blue(),
        )
        await ctx.send(embed=embed)

    @commands.command()
    async def skip(self, ctx):
        """Skip the current song."""
        if self.repeat_enabled:
            await ctx.send("Can't skip while Repeat is enabled!")
            return

        player = self._get_player(ctx.guild)
        if player and player.playing:
            await player.skip(force=True)

    @commands.command()
    async def stop(self, ctx):
        """Stops what's playing."""
        self._stopping = True
        while not self.music_queue.empty():
            self.music_queue.get_nowait()

        player = self._get_player(ctx.guild)
        if player and (player.playing or player.paused):
            player.queue.clear()
            await player.disconnect()

        guild_id = ctx.guild.id
        await self.invalidate_current_np(guild_id)
        self._signal_next_song()
        self._stopping = False

    @commands.command()
    async def pause(self, ctx):
        """Pauses the current song."""
        player = self._get_player(ctx.guild)
        if player:
            await player.pause(True)

    @commands.command()
    async def resume(self, ctx):
        """Resumes the current song."""
        player = self._get_player(ctx.guild)
        if player:
            await player.pause(False)

    @commands.command()
    async def repeat(self, ctx):
        """Enable/Disable repeat the current playing song."""
        self.repeat_enabled = not self.repeat_enabled
        message = "Current/Next Song Repeat is now {}."
        if self.repeat_enabled:
            await ctx.send(message.format("enabled"))
        else:
            self.repeated_entry = None
            await ctx.send(message.format("disabled"))

    @commands.command()
    async def loop(self, ctx):
        """Enable/Disable looping the current music queue."""
        self.loop_enabled = not self.loop_enabled
        message = "Music Looping is now {}."
        if self.loop_enabled:
            await ctx.send(message.format("enabled"))
        else:
            await ctx.send(message.format("disabled"))

    async def cog_load(self) -> None:
        settings = getattr(self.bot, "lavalink_settings", None)
        if settings and settings.enabled:
            self._lavalink_bootstrap_task = asyncio.create_task(self._bootstrap_lavalink())
            log.info("Lavalink bootstrap deferred until Discord is ready (music cog only).")

    async def shutdown_lavalink(self) -> None:
        if self._lavalink_shutdown_done:
            return
        self._lavalink_shutdown_done = True

        task = self._lavalink_bootstrap_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        server = getattr(self.bot, "lavalink_server", None)
        if server is not None:
            await server.stop()

    async def _bootstrap_lavalink(self) -> None:
        from cogs.utils.lavalink_client import connect_lavalink_with_retry

        await self.bot.wait_until_ready()

        settings = getattr(self.bot, "lavalink_settings", None)
        if settings is None or not settings.enabled:
            return

        server = getattr(self.bot, "lavalink_server", None)
        if server is None:
            log.warning("Lavalink manager missing; skipping music bootstrap.")
            return

        if settings.auto_start:
            try:
                started = await server.start(wait=False)
                if not started:
                    log.warning(
                        "Lavalink auto-start did not spawn a process; "
                        "will still try connecting in case another instance is running."
                    )
            except Exception as e:
                log.exception("Failed to start Lavalink: %s", e)

        connected = await connect_lavalink_with_retry(self.bot, load_credentials())
        if connected:
            log.info("Lavalink bootstrap finished; music is available.")
        else:
            log.warning("Lavalink bootstrap finished without a Wavelink connection; music is unavailable.")

    async def cog_unload(self):
        await self.shutdown_lavalink()

        while not self.music_queue.empty():
            self.music_queue.get_nowait()

        for voice_client in self.bot.voice_clients:
            try:
                await voice_client.disconnect()
            except Exception:
                pass

    @play.before_invoke
    @join.before_invoke
    @play_shuffle.before_invoke
    @play_playlist.before_invoke
    async def ensure_voice(self, ctx):
        if not self._lavalink_ready():
            await ctx.send("Lavalink is not connected. Bot owner: `!lavalink start`")
            raise commands.CommandError("Lavalink not connected.")

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("You are not connected to a voice channel.")
            raise commands.CommandError("Author not connected to a voice channel.")

        player = self._get_player(ctx.guild)
        author_channel = ctx.author.voice.channel
        if player is not None and self._needs_reconnect(ctx.guild, player):
            log.warning(
                "Ignoring stale voice client in %s; joining %s",
                ctx.guild.name,
                author_channel.name,
            )
            await self._drop_voice_client(ctx.guild)
            player = None
        elif ctx.guild.voice_client is not None and player is None:
            await self._drop_voice_client(ctx.guild)

        if player is None:
            if not await self._connect_to_author(ctx):
                await ctx.send("Failed to join your voice channel.")
                raise commands.CommandError("Failed to join voice channel.")
            await self.reset_player_controls()
        elif author_channel != player.channel:
            await player.move_to(author_channel, self_deaf=True, self_mute=False)
            await self.reset_player_controls()

    @resume.before_invoke
    @pause.before_invoke
    @stop.before_invoke
    @skip.before_invoke
    @volume.before_invoke
    async def ensure_voice_connected(self, ctx):
        if ctx.voice_client is None:
            await ctx.send("Not connected to a voice channel.")
            raise commands.CommandError("Bot is not connected to a voice channel")

    @stop.after_invoke
    @skip.after_invoke
    @resume.after_invoke
    @pause.after_invoke
    @volume.after_invoke
    @repeat.after_invoke
    @loop.after_invoke
    async def thumbs_up(self, ctx):
        await ctx.message.add_reaction('👍')

    @commands.command()
    async def count(self, ctx):
        """Current VC member count (Mostly for debug)."""
        voice_states = ctx.voice_client.channel.voice_states
        await ctx.send("{} people detected as connected to this channel".format(len(voice_states)))


async def setup(bot):
    credentials = load_credentials()
    bot.lavalink_settings = load_lavalink_settings(credentials)
    bot.lavalink_server = LavalinkServerManager(bot.lavalink_settings, credentials)
    await bot.add_cog(Music(bot))