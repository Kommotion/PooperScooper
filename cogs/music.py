from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
from typing import Optional

import discord
import wavelink
from discord.ext import commands, tasks
from discord.ext.commands import Cog

log = logging.getLogger(__name__)

ONE_MEMBER = 1
DEFAULT_VOLUME = 100  # Wavelink/Lavalink scale: 100 = 100%, max 1000

ASSETS_DIR = Path(__file__).resolve().parent / "assets" / "music"
SPINNING_DISC_FILE = "spinning_disc.gif"
STATIC_DISC_FILE = "vinyl_record.png"


class MusicEntry:
    def __init__(self, track: wavelink.Playable, ctx: commands.Context):
        self.track = track
        self.ctx = ctx


class NowPlayingView(discord.ui.View):
    """View with control buttons for the Now Playing embed. Only one active per guild."""

    def __init__(self, cog: "Music", entry: MusicEntry, message: discord.Message = None, timeout=3600):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.entry = entry
        self.message = message
        for child in self.children:
            cid = getattr(child, "custom_id", None)
            if cid == "np_repeat":
                child.style = discord.ButtonStyle.primary if cog.repeat_enabled else discord.ButtonStyle.secondary
            elif cid == "np_loop":
                child.style = discord.ButtonStyle.primary if cog.loop_enabled else discord.ButtonStyle.secondary
            elif cid == "np_shuffle":
                child.style = discord.ButtonStyle.primary if cog.shuffle_mode else discord.ButtonStyle.secondary

    def _check_voice(self, interaction):
        if not self.cog._same_voice_check(interaction):
            return False
        return True

    def _is_current_np(self, interaction):
        return self.cog.current_np_message.get(interaction.guild.id) == self.message

    async def _refresh_embed(self, interaction):
        if not self.message or not self._is_current_np(interaction):
            return
        player = interaction.guild.voice_client
        is_paused = player.paused if isinstance(player, wavelink.Player) else False
        embed = self.cog.now_playing_embed(self.entry, is_paused=is_paused)
        files = self.cog.disc_files(is_playing=not is_paused)
        new_view = NowPlayingView(self.cog, self.entry, self.message)
        try:
            await self.message.edit(embed=embed, view=new_view, attachments=files)
        except discord.NotFound:
            pass

    async def on_timeout(self):
        if self.message:
            guild_id = self.message.guild.id
            self.cog.current_np_message.pop(guild_id, None)
            try:
                await self.message.edit(view=None)
            except (discord.NotFound, discord.HTTPException):
                pass

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="np_skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_voice(interaction):
            await interaction.response.send_message("You must be in the same voice channel to control playback.", ephemeral=True)
            return
        if not self._is_current_np(interaction):
            await interaction.response.send_message("This player is no longer active.", ephemeral=True)
            return
        await self.cog._button_skip(interaction)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="np_stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_voice(interaction):
            await interaction.response.send_message("You must be in the same voice channel to control playback.", ephemeral=True)
            return
        if not self._is_current_np(interaction):
            await interaction.response.send_message("This player is no longer active.", ephemeral=True)
            return
        await self.cog._button_stop(interaction)

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.secondary, custom_id="np_pause")
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_voice(interaction):
            await interaction.response.send_message("You must be in the same voice channel to control playback.", ephemeral=True)
            return
        if not self._is_current_np(interaction):
            await interaction.response.send_message("This player is no longer active.", ephemeral=True)
            return
        await self.cog._button_pause(interaction)
        await self._refresh_embed(interaction)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.success, custom_id="np_resume")
    async def resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_voice(interaction):
            await interaction.response.send_message("You must be in the same voice channel to control playback.", ephemeral=True)
            return
        if not self._is_current_np(interaction):
            await interaction.response.send_message("This player is no longer active.", ephemeral=True)
            return
        await self.cog._button_resume(interaction)
        await self._refresh_embed(interaction)

    @discord.ui.button(emoji="🔂", style=discord.ButtonStyle.secondary, custom_id="np_repeat")
    async def repeat_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_voice(interaction):
            await interaction.response.send_message("You must be in the same voice channel to control playback.", ephemeral=True)
            return
        if not self._is_current_np(interaction):
            await interaction.response.send_message("This player is no longer active.", ephemeral=True)
            return
        await self.cog._button_repeat(interaction)
        await self._refresh_embed(interaction)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="np_loop")
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_voice(interaction):
            await interaction.response.send_message("You must be in the same voice channel to control playback.", ephemeral=True)
            return
        if not self._is_current_np(interaction):
            await interaction.response.send_message("This player is no longer active.", ephemeral=True)
            return
        await self.cog._button_loop(interaction)
        await self._refresh_embed(interaction)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="np_shuffle")
    async def shuffle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_voice(interaction):
            await interaction.response.send_message("You must be in the same voice channel to control playback.", ephemeral=True)
            return
        if not self._is_current_np(interaction):
            await interaction.response.send_message("This player is no longer active.", ephemeral=True)
            return
        await self.cog._button_shuffle(interaction)
        await self._refresh_embed(interaction)


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
        self._stopping = False

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

    async def invalidate_current_np(self, guild_id: int, entry: MusicEntry = None):
        message = self.current_np_message.pop(guild_id, None)
        if not message:
            return
        try:
            if entry is not None:
                ended_embed = self.now_playing_ended_embed(entry)
                await message.edit(
                    embed=ended_embed,
                    view=None,
                    attachments=self.disc_files(is_playing=False),
                )
            else:
                await message.edit(view=None)
        except (discord.NotFound, discord.HTTPException):
            pass

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
        if self.current_np_message.get(player.guild.id) is None and not self.repeat_enabled:
            return
        self._signal_next_song()

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload):
        log.error("Track exception: %s", payload.exception)
        self._signal_next_song()

    @tasks.loop(seconds=1)
    async def music_player(self):
        self.next_song.clear()
        entry = await self.get_entry()

        if self.loop_enabled and not self.repeat_enabled:
            await self.music_queue.put(entry)

        if await self.bot_is_alone(entry.ctx):
            return

        player = self._get_player(entry.ctx.guild)
        if not player:
            log.error("No Wavelink player for guild %s", entry.ctx.guild.id)
            self._signal_next_song()
            return

        try:
            await self.invalidate_current_np(entry.ctx.guild.id)
            embed = self.now_playing_embed(entry)
            view = NowPlayingView(self, entry)
            msg = await entry.ctx.send(
                embed=embed,
                view=view,
                files=self.disc_files(is_playing=True),
            )
            view.message = msg
            self.current_np_message[entry.ctx.guild.id] = msg

            await player.play(entry.track, volume=DEFAULT_VOLUME)
            if self.repeat_enabled:
                self.repeated_entry = entry
        except Exception as e:
            log.error("Unexpected error while playing %s: %s", entry.track.uri, e)
            await self.error_playing_embed(entry)
            self.repeated_entry = None
            self._signal_next_song()
            return

        await self.next_song.wait()
        await self.invalidate_current_np(entry.ctx.guild.id, entry=entry)

    @tasks.loop(seconds=30)
    async def idle_timeout(self):
        for voice_client in self.bot.voice_clients:
            if len(voice_client.channel.voice_states) <= ONE_MEMBER:
                guild_id = voice_client.guild.id
                await voice_client.disconnect()
                await self.invalidate_current_np(guild_id)

    @idle_timeout.before_loop
    async def before_timeout(self):
        await self.bot.wait_until_ready()

    async def bot_is_alone(self, ctx):
        number_of_members = len(ctx.voice_client.channel.voice_states)
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

    def now_playing_embed(self, entry: MusicEntry, is_paused=False):
        track = entry.track
        is_playing = not is_paused
        disc_url = self.disc_attachment_url(is_playing=is_playing)

        if is_paused:
            status_line = '⏸️ **Paused**'
            colour = discord.Colour.orange()
        else:
            status_line = '▶️ **Playing**'
            colour = discord.Colour(0x1DB954)

        embed = discord.Embed(colour=colour, timestamp=discord.utils.utcnow())
        embed.set_author(name='Now Playing', icon_url=disc_url)
        embed.set_image(url=disc_url)
        embed.add_field(name='Track', value=track.title, inline=False)
        if track.author:
            embed.add_field(name='Artist', value=track.author, inline=True)
        embed.add_field(name='Status', value=status_line, inline=True)
        embed.add_field(name='In queue', value=str(self.music_queue.qsize()), inline=True)
        embed.add_field(name='Requested by', value=entry.ctx.message.author.mention, inline=True)
        embed.add_field(
            name='Mode',
            value=' • '.join(
                s for s, on in [
                    ('🔂 Repeat', self.repeat_enabled),
                    ('🔁 Loop', self.loop_enabled),
                    ('🔀 Shuffle', self.shuffle_mode),
                ] if on
            ) or '—',
            inline=False,
        )
        if track.uri:
            embed.add_field(name='Link', value=track.uri, inline=False)
        if track.artwork:
            embed.set_thumbnail(url=track.artwork)
        embed.set_footer(text='Use the buttons below to control playback')
        return embed

    def now_playing_ended_embed(self, entry: MusicEntry):
        track = entry.track
        embed = discord.Embed(
            colour=discord.Colour.dark_gray(),
            timestamp=discord.utils.utcnow(),
        )
        disc_url = self.disc_attachment_url(is_playing=False)
        embed.set_author(name='Playback ended', icon_url=disc_url)
        embed.set_image(url=disc_url)
        embed.add_field(name='Last played', value=track.title, inline=False)
        embed.add_field(name='Requested by', value=entry.ctx.message.author.mention, inline=True)
        if track.artwork:
            embed.set_thumbnail(url=track.artwork)
        embed.set_footer(text='Queue another track to keep the party going')
        return embed

    def _same_voice_check(self, interaction):
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
        player = self._get_player(interaction.guild)
        if player and player.playing:
            await player.skip(force=True)
        await interaction.response.defer()

    async def _button_stop(self, interaction):
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
        await interaction.response.defer()

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

    async def cog_unload(self):
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
    async def ensure_voice(self, ctx):
        if not self._lavalink_ready():
            await ctx.send("Lavalink is not connected. Bot owner: `!lavalink start`")
            raise commands.CommandError("Lavalink not connected.")

        if ctx.voice_client is None:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect(
                    cls=wavelink.Player, self_deaf=True, self_mute=True,
                )
                await self.reset_player_controls()
            else:
                await ctx.send("You are not connected to a voice channel.")
                raise commands.CommandError("Author not connected to a voice channel.")
        elif ctx.author.voice:
            if ctx.author.voice.channel != ctx.voice_client.channel:
                await ctx.voice_client.move_to(ctx.author.voice.channel)
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
    await bot.add_cog(Music(bot))