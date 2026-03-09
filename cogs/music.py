import asyncio
import discord
import logging
import random
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import yt_dlp as youtube_dl
from discord.ext import commands, tasks
from discord.ext.commands import Cog
from cogs.utils.utils import load_credentials


log = logging.getLogger(__name__)

ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': True,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',  # bind to ipv4 since ipv6 addresses cause issues sometimes
    'force_generic_extractor': True,  # Handle tricky URLs
}
ffmpeg_options = {
    'options': '-vn',
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 10 -reconnect_on_network_error 1'
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)
ONE_MEMBER = 1
credentials = load_credentials()
DEFAULT_VOLUME = 0.15

# Now Playing embed images: spinning when playing, static when paused/ended
SPINNING_DISC_GIF = "https://i.makeagif.com/media/5-01-2016/eEcTQ8.gif"
STATIC_DISC_URL = "https://upload.wikimedia.org/wikipedia/commons/2/22/Vinyl_record.png"


class MusicEntry:
    def __init__(self, url, voice_client: discord.VoiceClient, ctx: commands.Context, player=None):
        self.player = player
        self.voice_client = voice_client
        self.ctx = ctx
        self.url = url


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=DEFAULT_VOLUME):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))

        if 'entries' in data:
            # take first item from a playlist
            data = data['entries'][0]

        filename = data['url'] if stream else ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)


class NowPlayingView(discord.ui.View):
    """View with control buttons for the Now Playing embed. Only one active per guild."""

    def __init__(self, cog: "Music", entry: MusicEntry, message: discord.Message = None, timeout=3600):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.entry = entry
        self.message = message
        # Color repeat/loop/shuffle by state (primary = on)
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
        """True if this view's message is still the active Now Playing for this guild."""
        return self.cog.current_np_message.get(interaction.guild.id) == self.message

    async def _refresh_embed(self, interaction):
        """Update the Now Playing message to reflect current state (repeat, loop, paused)."""
        if not self.message or not self._is_current_np(interaction):
            return
        voice_client = interaction.guild.voice_client
        is_paused = voice_client.is_paused() if voice_client else False
        embed = self.cog.now_playing_embed(self.entry, is_paused=is_paused)
        new_view = NowPlayingView(self.cog, self.entry, self.message)
        try:
            await self.message.edit(embed=embed, view=new_view)
        except discord.NotFound:
            pass

    async def on_timeout(self):
        """When the view expires, remove buttons and clear the current NP reference."""
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
    """Commands for playing music in voice chat. """

    def __init__(self, bot):
        self.bot = bot
        self.music_queue = asyncio.Queue()
        self.next_song = asyncio.Event()
        self.music_player.start()
        self.idle_timeout.start()
        client_id = credentials['spotify_client_id']
        client_secret = credentials['spotify_secret']
        auth_manager = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
        self.spotipy = spotipy.Spotify(auth_manager=auth_manager)

        # Player Control Variables
        self.repeat_enabled = False
        self.repeated_entry = None
        self.loop_enabled = False
        self.shuffle_mode = False
        # One active Now Playing message per guild; invalidated when new song or bot disconnects
        self.current_np_message = {}

    async def reset_player_controls(self):
        self.repeat_enabled = False
        self.repeated_entry = None
        self.loop_enabled = False
        self.shuffle_mode = False

    async def invalidate_current_np(self, guild_id: int, entry: MusicEntry = None):
        """Remove buttons from the current Now Playing message. If entry is given (song ended), show 'ended' embed with static disc."""
        message = self.current_np_message.pop(guild_id, None)
        if not message:
            return
        try:
            if entry is not None:
                ended_embed = self.now_playing_ended_embed(entry)
                await message.edit(embed=ended_embed, view=None)
            else:
                await message.edit(view=None)
        except (discord.NotFound, discord.HTTPException):
            pass

    async def get_entry(self):
        # If repeat is enabled get the stored repeated entry if there is one
        if self.repeat_enabled:
            entry = self.repeated_entry if self.repeated_entry else await self.music_queue.get()
            if not self.repeated_entry:
                self.repeated_entry = entry
            return entry
        # Shuffle mode: next song is a random pick from the queue
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
        entry = await self.music_queue.get()
        return entry

    @tasks.loop(seconds=1)
    async def music_player(self):
        self.next_song.clear()
        entry = await self.get_entry()

        # If loop is enabled, put it back into the queue
        if self.loop_enabled and not self.repeat_enabled:
            await self.music_queue.put(entry)

        if await self.bot_is_alone(entry.ctx):
            return

        try:
            entry.player = await YTDLSource.from_url(entry.url, loop=self.bot.loop, stream=True)
            await self.invalidate_current_np(entry.ctx.guild.id)
            embed = self.now_playing_embed(entry)
            view = NowPlayingView(self, entry)
            msg = await entry.ctx.send(embed=embed, view=view)
            view.message = msg
            self.current_np_message[entry.ctx.guild.id] = msg
            entry.voice_client.play(entry.player, after=self.play_next_entry)
            # If repeat was enabled, make sure that we store the current entry to the repeated entry
            if self.repeat_enabled:
                self.repeated_entry = entry
        except Exception as e:
            log.error(f"Unexpected error while playing {entry.url}: {e}")
            await self.error_playing_embed(entry)
            # If there was an error playing the song for some reason skip to the next song
            self.repeated_entry = None
            self.play_next_entry(e)

        await self.next_song.wait()
        # Song ended: remove buttons and show static "ended" state so only one NP is active
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
        """If bot is alone, but we are going to keep playing music, return True to stop playing music."""
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

    async def error_playing_embed(self, entry):
        embed = discord.Embed(
            title='Error While Playing:',
            description=entry.url,
            colour=discord.Colour.blue(),
        )
        await entry.ctx.send(embed=embed)

    def now_playing_embed(self, entry, is_paused=False):
        is_playing = not is_paused
        # Spinning disc when playing, static when paused
        disc_url = SPINNING_DISC_GIF if is_playing else STATIC_DISC_URL

        if is_paused:
            status_line = '⏸️ **Paused**'
            colour = discord.Colour.orange()
        else:
            status_line = '▶️ **Playing**'
            colour = discord.Colour(0x1DB954)  # Spotify green

        embed = discord.Embed(colour=colour, timestamp=discord.utils.utcnow())
        embed.set_author(name='Now Playing', icon_url=disc_url)
        embed.set_image(url=disc_url)
        embed.add_field(name='Track', value=entry.player.title, inline=False)
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
        webpage_url = self._get_value(entry, 'webpage_url')
        embed.add_field(name='Link', value=webpage_url, inline=False)
        thumb = self._get_value(entry, 'thumbnail')
        if thumb != 'No thumbnail specified':
            embed.set_thumbnail(url=thumb)
        embed.set_footer(text='Use the buttons below to control playback')
        return embed

    def now_playing_ended_embed(self, entry):
        """Embed shown when playback has ended (buttons removed); static disc."""
        embed = discord.Embed(
            colour=discord.Colour.dark_gray(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name='Playback ended', icon_url=STATIC_DISC_URL)
        embed.set_image(url=STATIC_DISC_URL)
        embed.add_field(name='Last played', value=entry.player.title, inline=False)
        embed.add_field(name='Requested by', value=entry.ctx.message.author.mention, inline=True)
        thumb = self._get_value(entry, 'thumbnail')
        if thumb != 'No thumbnail specified':
            embed.set_thumbnail(url=thumb)
        embed.set_footer(text='Queue another track to keep the party going')
        return embed

    def _get_value(self, entry, value):
        try:
            return entry.player.data[value]
        except KeyError:
            return 'No {} specified'.format(value)

    def _same_voice_check(self, interaction):
        """Returns True if the user is in the same voice channel as the bot."""
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
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_playing():
            voice_client.stop()
        await interaction.response.defer()

    async def _button_stop(self, interaction):
        while not self.music_queue.empty():
            self.music_queue.get_nowait()
        voice_client = interaction.guild.voice_client
        if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            voice_client.stop()
        guild_id = interaction.guild.id
        await voice_client.disconnect()
        await self.invalidate_current_np(guild_id)
        await interaction.response.defer()

    async def _button_pause(self, interaction):
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_playing():
            voice_client.pause()
            await interaction.response.defer()
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    async def _button_resume(self, interaction):
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_paused():
            voice_client.resume()
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

    def play_next_entry(self, error):
        log.warning('Player error: %s' % error) if error else None
        self.bot.loop.call_soon_threadsafe(self.next_song.set)

    @music_player.before_loop
    async def before_music(self):
        await self.bot.wait_until_ready()

    @commands.command()
    async def join(self, ctx):
        """Joins the voice channel. """
        pass

    @commands.group(invoke_without_command=True)
    async def play(self, ctx, *, url):
        """Plays a youtube url or spotify playlist/album."""
        await self._play(ctx, url)

    @play.command(name="shuffle")
    async def play_shuffle(self, ctx, *, url):
        """Plays a youtube url or spotify album/playlist and shuffles before playing."""
        await self._play(ctx, url, shuffle=True)

    @play.command(name="playlist")
    async def play_playlist(self, ctx, *, url):
        """Plays a YouTube playlist or Spotify playlist/album."""
        await self._play(ctx, url)

    def get_from_youtube_playlist(self, url):
        """Extracts a list of video URLs from a YouTube playlist."""
        try:
            # Configure yt_dlp to extract only info without downloading
            playlist_info = ytdl.extract_info(url, download=False)
            if 'entries' not in playlist_info:
                raise Exception("Not a valid YouTube playlist")

            # Extract video URLs from playlist entries
            return_list = [entry['webpage_url'] for entry in playlist_info['entries'] if entry]
            return return_list
        except Exception as e:
            log.warning(f"Failed to parse YouTube playlist: {e}")
            return []

    async def _play(self, ctx, url, shuffle=False):
        async with ctx.typing():
            if 'spotify' in url:
                music_list = self.get_from_spotify(url)
            elif 'youtube.com/playlist' in url or 'list=' in url:
                music_list = self.get_from_youtube_playlist(url)
                if not music_list:
                    embed = discord.Embed(
                        title='Error',
                        description='Failed to parse YouTube playlist. It may be private, unavailable, or not a valid playlist.',
                        colour=discord.Colour.red()
                    )
                    await ctx.send(embed=embed)
                    return
            else:
                # Single item in music list
                music_list = list()
                music_list.append(url)

            if shuffle:
                random.shuffle(music_list)

            for url in music_list:
                entry = MusicEntry(url, ctx.voice_client, ctx)
                await self.music_queue.put(entry)

            music_list_length = len(music_list)
            description = url if music_list_length == 1 else '{} songs'.format(music_list_length)

            embed = discord.Embed(
                title='Queued up',
                description=description,
                colour=discord.Colour.blue()
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

    def get_from_spotify(self, url):
        """Use Spotipy to get a list of songs from a spotify. """
        try:
            return_list = self._get_playlist_from_spotify(url)
        except spotipy.SpotifyException:
            return_list = self._get_playlist_from_album(url)

        return return_list

    def _get_playlist_from_spotify(self, url):
        fields = 'items.track.name,items.track.artists'
        music_list = self.spotipy.playlist_items(url, fields=fields, additional_types=['track'])
        return_list = list()
        for track in music_list['items']:
            url_info = ''
            url_info += '{} '.format(track['track']['name'])
            for artist in track['track']['artists']:
                url_info += '{} '.format(artist['name'])
            url_info += 'song music'
            return_list.append(url_info)
        return return_list

    def _get_playlist_from_album(self, url):
        music_list = self.spotipy.album_tracks(url)
        return_list = list()
        for track in music_list['items']:
            url_info = ''
            url_info += '{} '.format(track['name'])
            for artist in track['artists']:
                url_info += '{} '.format(artist['name'])
            url_info += 'song music'
            return_list.append(url_info)
        return return_list

    @commands.command()
    async def volume(self, ctx, volume: int):
        """Adjust the bot's voice volume (15 is the default)."""
        original = int(ctx.voice_client.source.volume * 100)
        ctx.voice_client.source.volume = volume / 100

        description = '{} -> {}'.format(str(original), str(volume))
        embed = discord.Embed(
            title='Player Volume 🔊',
            description=description,
            colour=discord.Colour.blue()
        )

        await ctx.send(embed=embed)

    @commands.command()
    async def skip(self, ctx):
        """Skip the current song."""
        if self.repeat_enabled:
            await ctx.send("Can't skip while Repeat is enabled!")
            return

        if ctx.voice_client.is_playing():
            ctx.voice_client.stop()

    @commands.command()
    async def stop(self, ctx):
        """Stops what's playing."""
        while not self.music_queue.empty():
            self.music_queue.get_nowait()

        if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
            ctx.voice_client.stop()

        guild_id = ctx.guild.id
        await ctx.voice_client.disconnect()
        await self.invalidate_current_np(guild_id)

    @commands.command()
    async def pause(self, ctx):
        """Pauses the current song."""
        ctx.voice_client.pause()

    @commands.command()
    async def resume(self, ctx):
        """Pauses the current song."""
        ctx.voice_client.resume()

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
        """Clear music queue and disconnect from all voice channels when unloading the cog"""
        while not self.music_queue.empty():
            self.music_queue.get_nowait()

        for voice_client in self.bot.voice_clients:
            try:
                await voice_client.disconnect()
            except:
                pass


    @play.before_invoke
    @join.before_invoke
    @play_shuffle.before_invoke
    async def ensure_voice(self, ctx):
        if ctx.voice_client is None:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect(self_deaf=True, self_mute=True)
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
