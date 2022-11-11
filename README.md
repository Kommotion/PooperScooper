## PooperScooper
Personal Discord bot using the discord.py library by Rapptz (https://github.com/Rapptz/discord.py)

This bot was intended to be a single server bot and was not developed to be in multiple servers.

## Key Features
1. Music
2. Gametime
3. GrammarPolice

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

## Running
Run the following with administrator priviledges

`python PooperScooper.py`

Add `-d` switch to set logging level to debug

## Links
* [Discord.py](https://github.com/Rapptz/discord.py)
* [Discord.py Documentation](https://discordpy.readthedocs.io/en/latest/index.html)
* [Python](https://www.python.org/downloads)