## PooperScooper
Personal Discord bot using the discord.py library by Rapptz (https://github.com/Rapptz/discord.py)

This bot was intended to be a single server bot and was not developed to be in multiple servers.
Feel free to use this as a reference for your own bot.

Note: Some stuff will be hardcoded to particular servers. Change these if
your intention is to use this in your own server.

## Key Features
1. Music
2. Gametime tracking
3. ImageDiffusion
4. GrammarPolice (Limited, no LLM being used)
5. Role Selection by Emoji
6. Birthday Tracking (Opt in)
7. Poll
8. Bingo Card Generation

## Installing

1. **Install Python 3.11 or higher**

Required for Wavelink 3 and discord.py 2.7+
   
2. **Set up venv**

` python3.8 -m venv venv`

3. **Install dependencies**

`pip install -U -r requirements.txt`

4. **Create Spotify and Discord integrations**

Create your integrations for Spotify and Discord:

* https://developer.spotify.com/dashboard/

* https://discord.com/developers/applications

5. **Configure Credentials**

Copy `config.example.json` to `config.json` and fill in your credentials. **Do not commit `config.json`.**

6. **Configure Lavalink (Music)**

Music uses a local Lavalink server. The bot can **start and stop it automatically** when configured.

Requirements: **Java 17+**, **FFmpeg** on PATH, **yt-dlp** on PATH.

One-time setup (downloads `Lavalink.jar`):

```powershell
cd lavalink
.\setup.ps1
```

`config.json` Lavalink section (see `config.example.json`):

```json
"lavalink": {
  "enabled": true,
  "managed": true,
  "auto_start": true,
  "host": "localhost",
  "port": 2333,
  "password": "choose-a-strong-password",
  "java_executable": "java",
  "directory": "lavalink",
  "ytdlp_executable": "",
  "deezer_arl": ""
}
```

- `managed: true` — bot starts/stops Lavalink with itself
- `managed: false` — you run Lavalink externally (for shared/public deployments)
- `auto_start: false` — bot owner starts manually with `!lavalink start`

Owner commands: `!lavalink`, `!lavalink start`, `!lavalink stop`

Secrets (Spotify, Lavalink password) are written to `lavalink/application.yml` at runtime from `config.json` — never commit that file.

7. **Configure FFMPEG**

Download FFMPEG and add the executable to your environment variables
* https://www.ffmpeg.org/download.html

8. **Install Image Diffusion Models**

NOTE: This requires a powerful host platform with at least 4GB+ of VRAM recommended. I recommend doing more research on
OS/hardware requirements before enabling the image diffusion cog. Currently, I have it running on 8GB VRAM GPU and 64GB RAM system
while offloading some of the processing to the CPU at the time of this writing.

Check the image diffusion cog for all the models that you might need. All models used can be found here:
* https://civitai.com/
* https://huggingface.co/

You will need Pytorch installation for your setup: https://pytorch.org/get-started/locally/

The cog currently expects that your models are cached locally on your system. I definitely recommend doing this
otherwise the huggingface library will download a model if it is not cached which can destroy your bandwidth limits.

## Running
Run the following with administrator privileges

`python PooperScooper.py`

Add `-d` switch to set logging level to debug

## Links
* [Discord.py](https://github.com/Rapptz/discord.py)
* [Discord.py Documentation](https://discordpy.readthedocs.io/en/latest/index.html)
* [Python](https://www.python.org/downloads)