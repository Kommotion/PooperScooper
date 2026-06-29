# Deployment

## Docker vs Windows Task Scheduler

Use **both**, depending on what you are running:

| Component | Docker | Windows native |
|-----------|--------|----------------|
| Discord bot + Lavalink | Good fit (`docker compose up -d`) | Also works (bot can manage Lavalink) |
| Palworld server (SteamCMD, RCON, process control) | Poor fit — needs Windows host access | **Required on Windows** |
| ComfyUI / GPU image generation | Possible with NVIDIA Container Toolkit | Typical setup today |
| Auto-start on boot | `restart: unless-stopped` in Compose | Task Scheduler |

**Recommendation for this repo's current feature set:**

- **Windows host:** Keep Palworld and ComfyUI native. Run the bot with Task Scheduler *or* Docker — Docker mainly replaces babysitting the bot/Lavalink process, not Palworld.
- **Linux / headless:** `docker compose` is a clean way to run bot + Lavalink. Disable Palworld cog or run Palworld on a separate Windows machine.

## Docker quick start

1. Copy `config.example.json` → `config.json` and fill in secrets (or use `.env`; see README).
2. Set `lavalink.managed: false` and `lavalink.host: lavalink` in `config.json` when using Compose.
3. Provide `lavalink/application.yml` (generate from the bot once, or copy from a working install).
4. Start:

```bash
docker compose up -d --build
```

Logs: `docker compose logs -f bot`

## Logs on native Windows

The bot uses a rotating log file, `pooperscooper.log`, keeping **10 files total** (current + 9 backups). When the active file exceeds 5 MB, the oldest backup is deleted.

## Runtime data

Guild/user JSON stores live under `data/` (gametime, birthdays, bingo, game picker). They are created automatically and migrated from the repo root on first boot if legacy files exist.