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
    'source_address': '0.0.0.0'  # bind to ipv4 since ipv6 addresses cause issues sometimes
}
ffmpeg_options = {
    'options': '-vn',
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)
ONE_MEMBER = 1
credentials = load_credentials()
DEFAULT_VOLUME = 0.15


class MusicEntry:
    def __init__(self, url, voice_client, ctx, player=None):
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

    async def reset_player_controls(self):
        self.repeat_enabled = False
        self.repeated_entry = None
        self.loop_enabled = False

    async def get_entry(self):
        # If repeat is enabled get the stored repeated entry if there is one
        if self.repeat_enabled:
            entry = self.repeated_entry if self.repeated_entry else await self.music_queue.get()
            if not self.repeated_entry:
                self.repeated_entry = entry
        else:
            entry = await self.music_queue.get()
        return entry

    @tasks.loop(seconds=1)
    async def music_player(self):
        self.next_song.clear()
        entry = await self.get_entry()

        # If loop is enabled, put it back into the queue
        if self.loop_enabled:
            await self.music_queue.put(entry)

        if await self.bot_is_alone(entry.ctx):
            return

        try:
            entry.player = await YTDLSource.from_url(entry.url, loop=self.bot.loop, stream=True)
            embed = self.now_playing_embed(entry)
            await entry.ctx.send(embed=embed)
            entry.voice_client.play(entry.player, after=self.play_next_entry)
            # If repeat was enabled, make sure that we store the current entry to the repeated entry
            if self.repeat_enabled:
                self.repeated_entry = entry
        except Exception as e:
            await self.error_playing_embed(entry)
            # If there was an error playing the song for some reason skip to the next song
            self.repeated_entry = None
            self.play_next_entry(e)

        await self.next_song.wait()

    @tasks.loop(seconds=30)
    async def idle_timeout(self):
        for voice_client in self.bot.voice_clients:
            if len(voice_client.channel.voice_states) <= ONE_MEMBER:
                await voice_client.disconnect()

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
            await ctx.voice_client.disconnect()
            return True
        return False

    async def error_playing_embed(self, entry):
        embed = discord.Embed(
            title='Error While Playing:',
            description=entry.url,
            colour=discord.Colour.blue(),
        )
        await entry.ctx.send(embed=embed)

    def now_playing_embed(self, entry):
        title = '▶️ Now Playing 🎵'
        if self.repeat_enabled:
            title += ' 🔂'
        elif self.loop_enabled:
            title += ' 🔁'

        embed = discord.Embed(
            title=title,
            description=entry.player.title,
            colour=discord.Colour.blue(),
        )

        name = 'Artist'
        value = self._get_value(entry, 'artist')
        embed.add_field(name=name, value=value, inline=True)

        name = 'Album'
        value = self._get_value(entry, 'album')
        embed.add_field(name=name, value=value, inline=True)

        name = 'Requester'
        value = entry.ctx.message.author
        embed.add_field(name=name, value=value, inline=True)

        name = 'Songs in Queue'
        value = str(self.music_queue.qsize())
        embed.add_field(name=name, value=value, inline=True)

        name = 'Loop'
        value = 'Enabled' if self.loop_enabled else 'Disabled'
        embed.add_field(name=name, value=value, inline=True)

        name = 'Repeat'
        value = 'Enabled' if self.repeat_enabled else 'Disabled'
        embed.add_field(name=name, value=value, inline=True)

        name = 'Source'
        webpage_url = self._get_value(entry, 'webpage_url')
        value = '{}'.format(webpage_url)
        embed.add_field(name=name, value=value, inline=False)

        url = self._get_value(entry, 'thumbnail')
        if url != 'No thumbnail specified':
            embed.set_thumbnail(url=url)

        return embed

    def _get_value(self, entry, value):
        try:
            return entry.player.data[value]
        except KeyError:
            return 'No {} specified'.format(value)

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

    async def _play(self, ctx, url, shuffle=False):
        async with ctx.typing():
            if 'spotify' in url:
                music_list = self.get_from_spotify(url)
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
        await ctx.voice_client.disconnect()

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

    @play.before_invoke
    @join.before_invoke
    @play_shuffle.before_invoke
    async def ensure_voice(self, ctx):
        if ctx.voice_client is None:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect()
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
