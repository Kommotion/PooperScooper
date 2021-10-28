import asyncio
import discord
import json
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import youtube_dl
from discord.ext import commands, tasks
from discord.ext.commands import Cog
from pprint import pprint


ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
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


def load_credentials():
    with open('config.json') as f:
        return json.load(f)


credentials = load_credentials()


class MusicEntry:
    def __init__(self, url, voice_client, ctx, player=None):
        self.player = player
        self.voice_client = voice_client
        self.ctx = ctx
        self.url = url


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.3):
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
        client_id = credentials['spotify_client_id']
        client_secret = credentials['spotify_secret']
        auth_manager = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
        self.spotipy = spotipy.Spotify(auth_manager=auth_manager)

    @tasks.loop(seconds=1)
    async def music_player(self):
        self.next_song.clear()
        entry = await self.music_queue.get()
        entry.player = await YTDLSource.from_url(entry.url, loop=self.bot.loop, stream=True)
        embed = self.now_playing_embed(entry)
        await entry.ctx.send(embed=embed)
        entry.voice_client.play(entry.player, after=self.play_next_entry)
        await self.next_song.wait()

    def now_playing_embed(self, entry):
        embed = discord.Embed(
            title='Now Playing 🎵',
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

        name = 'Source'
        webpage_url = self._get_value(entry, 'webpage_url')
        value = '[{}]({})'.format(webpage_url, webpage_url)
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
        print('Player error: %s' % error) if error else None
        self.bot.loop.call_soon_threadsafe(self.next_song.set)

    @music_player.before_loop
    async def before_music(self):
        await self.bot.wait_until_ready()

    @commands.command()
    async def join(self, ctx):
        """Joins the voice channel. """
        pass

    @commands.command()
    async def play(self, ctx, *, url):
        """Joins the channel and Plays something from youtube. Supports Spotify Playlists URLs. """
        async with ctx.typing():
            if 'spotify' in url:
                music_list = self.get_playlist_from_spotify(url)
            else:
                # Single item in music list
                music_list = list()
                music_list.append(url)

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

    def get_playlist_from_spotify(self, url):
        """Use Spotipy to get a list of songs from a spotify playlist. """
        fields = 'items.track.name,items.track.artists'
        music_list = self.spotipy.playlist_items(url, fields=fields, additional_types=['track'])
        return_list = list()
        for track in music_list['items']:
            url_info = ''
            url_info += '{} '.format(track['track']['name'])
            for artist in track['track']['artists']:
                url_info += '{} '.format(artist['name'])
            return_list.append(url_info)
        return return_list

    @commands.command()
    async def volume(self, ctx, volume: int):
        """Adjust the bot's voice volume. """
        ctx.voice_client.source.volume = volume / 100

        embed = discord.Embed(
            title='Player Volume 🔊',
            description=str(volume),
            colour=discord.Colour.blue()
        )

        await ctx.send(embed=embed)

    @commands.command()
    async def skip(self, ctx):
        """Skip the current song. """
        if ctx.voice_client.is_playing():
            ctx.voice_client.stop()

    @commands.command()
    async def stop(self, ctx):
        """Stops what's playing. """
        while not self.music_queue.empty():
            self.music_queue.get_nowait()
        await ctx.voice_client.disconnect()

    @commands.command()
    async def pause(self, ctx):
        """Pauses the current song. """
        ctx.voice_client.pause()

    @commands.command()
    async def resume(self, ctx):
        """Pauses the current song. """
        ctx.voice_client.resume()

    @play.before_invoke
    @join.before_invoke
    async def ensure_voice(self, ctx):
        if ctx.voice_client is None:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect()
            else:
                await ctx.send("You are not connected to a voice channel.")
                raise commands.CommandError("Author not connected to a voice channel.")
        elif ctx.author.voice:
            if ctx.author.voice.channel != ctx.voice_client.channel:
                await ctx.voice_client.move_to(ctx.author.voice.channel)

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
    async def thumbs_up(self, ctx):
        await ctx.message.add_reaction('👍')


def setup(bot):
    bot.add_cog(Music(bot))
