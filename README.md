## PooperScooper
Personal Discord bot using the discord.py library by Rapptz (https://github.com/Rapptz/discord.py)

This bot was intended to be a single server bot and was not developed to be in multiple servers.
Feel free to use this as a reference for your own bot.

Per-guild settings use `server_configs/<guild_id>.json`. See [docs/SERVER_CONFIGS.md](docs/SERVER_CONFIGS.md).

Runtime JSON data (gametime, birthdays, bingo, game picker) is stored under `data/`.

## Key Features
1. Music (Lavalink / Wavelink)
2. Gametime tracking (shared playtime across servers)
3. ImageDiffusion (ComfyUI)
4. Reaction roles by emoji
5. Birthday tracking (opt in)
6. Bingo card generation
7. Game picker & random commands
8. Palworld server management (MTS)

## Installing

1. **Install Python 3.11 or higher**

Required for Wavelink 3 and discord.py 2.7+.

2. **Set up venv**

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

3. **Install dependencies**

Core only:

```powershell
pip install -U -r requirements.txt
```

Full install (bingo, image helpers, palworld logging):

```powershell
pip install -U -r requirements-all.txt
```

Or install optional sets from `pyproject.toml`:

```powershell
pip install -e ".[bingo]"
pip install -e ".[image]"
pip install -e ".[palworld]"
pip install -e ".[all]"
```

4. **Create Spotify and Discord integrations**

Create your integrations for Spotify and Discord:

* https://developer.spotify.com/dashboard/

* https://discord.com/developers/applications

5. **Configure Credentials**

Copy `config.example.json` to `config.json` and fill in your credentials. **Do not commit `config.json`.**

Environment variables override `config.json` when set (useful for Docker):

| Variable | Overrides |
|----------|-----------|
| `DISCORD_TOKEN` | `token` |
| `DISCORD_CLIENT_ID` | `client_id` |
| `SPOTIFY_CLIENT_ID` | `spotify_client_id` |
| `SPOTIFY_SECRET` | `spotify_secret` |
| `LAVALINK_HOST` | `lavalink.host` |
| `LAVALINK_PORT` | `lavalink.port` |
| `LAVALINK_PASSWORD` | `lavalink.password` |

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
- `managed: false` — you run Lavalink externally (for Docker or shared deployments)
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

9. **Configure per-guild settings**

Copy `server_configs/example.json` to `server_configs/<your_guild_id>.json` and fill in channel/role IDs. Full reference: [docs/SERVER_CONFIGS.md](docs/SERVER_CONFIGS.md).

For Palworld auto-restart/backup timers, copy `palworld_config.example.json` to `palworld_config.json`.
Enable Palworld commands per guild with `"palworld": { "enabled": true }` in that guild's server config.

## Running

```powershell
python PooperScooper.py
```

Add `-d` for debug logging.

Logs rotate automatically: `pooperscooper.log` plus up to 9 older backups (10 files total, 5 MB each).

Owner/admin: `!status` shows bot health (Lavalink, ComfyUI, Palworld, etc.).

## Docker (bot + Lavalink)

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). Docker is a good fit for the bot and Lavalink; Palworld and ComfyUI still typically run on the Windows host.

```bash
docker compose up -d --build
```

Set `lavalink.managed: false` and `lavalink.host: lavalink` in `config.json` when using Compose.

## Tests

```powershell
pip install pytest
pytest tests/test_smoke.py -q
```

## Links
* [Discord.py](https://github.com/Rapptz/discord.py)
* [Discord.py Documentation](https://discordpy.readthedocs.io/en/latest/index.html)
* [Python](https://www.python.org/downloads)