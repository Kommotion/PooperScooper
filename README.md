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

1. **Install Python 3.8 or higher**

This is a requirement for Discord.py
   
2. **Set up venv**

` python3.8 -m venv venv`

3. **Install dependencies**

`pip install -U -r requirements.txt`

4. **Create Spotify and Discord integrations**

Create your integrations for Spotify and Discord:

* https://developer.spotify.com/dashboard/

* https://discord.com/developers/applications

5. **Configure Credentials**

Add credentials to config.json from Spotify/Discord. Place in root dir of bot.
```j
{
  "token": "",
  "client_id": "",
  "spotify_client_id": "",
  "spotify_secret": ""
}
```

6. **Configure FFMPEG**

Download FFMPEG and add the executable to your environment variables
* https://www.ffmpeg.org/download.html

7. **Install Image Diffusion Models**

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