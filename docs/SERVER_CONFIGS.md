# Server Configs

Per-guild settings live in `server_configs/<guild_id>.json`. The guild ID is the numeric Discord server ID (Developer Mode → right-click server → Copy Server ID).

Copy `server_configs/example.json` to `server_configs/<your_guild_id>.json` and edit the values.

## File layout

```
server_configs/
  example.json                 # Template (not loaded automatically)
  932057681307512922.json      # Menaces to Sobriety
  1045144253627637830.json     # PooperScooper Support
```

Configs are loaded by `cogs/utils/server_config.py`. Changes take effect on bot restart (configs are cached in memory).

## Sections

### Top level

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | bool | If `false`, guild-specific features that read this file are disabled. |
| `guild_name` | string | Display name for logs/docs only. |

### `image_diffusion`

Used by `cogs/imagediffusion.py`.

| Field | Type | Description |
|-------|------|-------------|
| `allowed_channel_ids` | int[] | Channel IDs where `/imagegen` works. |
| `moderator_role_id` | int \| null | Role that can delete generated images (in addition to the author). |

### `reaction_roles`

Used by `cogs/menacesroles.py`.

| Field | Type | Description |
|-------|------|-------------|
| `channel_id` | int \| null | Channel where reaction-role messages live. |
| `emoji_map` | object | Emoji name → role ID. Custom emoji names use the emoji string key. |

### `birthday`

Used by `cogs/birthdaytracker.py`.

| Field | Type | Description |
|-------|------|-------------|
| `announce_channel_id` | int \| null | Channel for birthday announcements (preferred). |
| `announce_channel_name` | string | Fallback channel name if ID is null (default: `general`). |
| `timezone` | string | IANA timezone for the daily birthday check (e.g. `America/Chicago`). Runs at local midnight. |

### `bingo`

Used by `cogs/bingo.py`.

| Field | Type | Description |
|-------|------|-------------|
| `card_title` | string | Title printed on generated bingo card images. |

### `music`

Used by `cogs/music.py` for Now Playing button permissions.

| Field | Type | Description |
|-------|------|-------------|
| `dj_role_id` | int \| null | Members with this role can control playback without being in the voice channel. |

### `palworld`

Used by `cogs/palworld.py` for guild enablement (commands remain MTS-only via code checks).

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | bool | Whether Palworld features are active for this guild. |
| `status_channel_id` | int \| null | Reserved for future automated status posts. |

## Palworld operational config (separate file)

Server process settings (auto-restart, backups, log rotation) are in **`palworld_config.json`** at the repo root, not in `server_configs/`.

Copy `palworld_config.example.json` → `palworld_config.json`.

RCON/Steam credentials remain in `cogs/utils/palworld_utils/palworld.json`.

## Adding a new guild

1. Copy `server_configs/example.json` to `server_configs/<guild_id>.json`.
2. Set `enabled: true` and fill in channel/role IDs.
3. Restart the bot (or `!reload_cog` for cogs that read config at runtime — most cache on load).
4. Run `!sync_commands` if you added slash commands scoped to that guild.

## Bot status

Run `!status` in Discord for uptime, Lavalink/Wavelink, ComfyUI queue, Palworld, and configured guild count.