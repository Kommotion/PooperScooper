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

Used by `cogs/palworld.py` to gate both `!palworld` prefix commands and `/palworld` slash commands.

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | bool | Whether Palworld features are active for this guild. |
| `status_channel_id` | int \| null | Reserved for future automated status posts. |

### `easter_eggs`

Used by `cogs/eastereggs.py`. Missing or disabled sections do nothing.

| Field | Type | Description |
|-------|------|-------------|
| `hooty_hoo.enabled` | bool | React when a message says "hooty hoo" or "hooty fucking hoot". |
| `hooty_hoo.emoji` | string | Custom emoji to react with, e.g. `<:salute:123>`. |
| `hooty_hoo.skip_channel_ids` | int[] | Channels (and threads under them) that do not get the reaction. |
| `hooty_hoo.channel_ids` | int[] | If set, only these channels get the reaction. Empty means every channel except the skip list. |
| `allow_webhooks` | bool | When true, webhook messages in this guild can trigger easter eggs. Default: false. |
| `do_not_redeem.enabled` | bool | Reply on a new help post in one forum. |
| `do_not_redeem.channel_id` | int \| null | Forum channel whose opening help posts get the reply. |
| `do_not_redeem.reply` | string | Reply text. Default: `DO NOT REDEEM`. |
| `announcement_party.enabled` | bool | React on drinking-night posts in the listed channels. |
| `announcement_party.channel_ids` | int[] | Channels (and threads under them) where the party reactions run. |
| `announcement_party.timezone` | string | IANA timezone used to decide whether a reminder falls on Saturday. |
| `announcement_party.reaction_count` | int | How many different emoji to add. Default: 5. |
| `announcement_party.emojis` | string[] | Pool of custom emoji. Each post gets a random subset. |

"ITS ABOUT THAT TIME" reacts on any day. A Saturday post reacts when it says any of reminder, tonight, headset, charge, or "get ready." Commands that start with `!` are ignored. The redeem reply only fires on the first message of a new post in that forum, and only when the title or body looks like a help request.

### `candy_hunt`

Optional overrides for the Halloween candy hunt (`cogs/candyhunt.py`). A missing section keeps the cog defaults. Invalid values are ignored.

The public slash command is `/candy`, registered only in Menaces to Sobriety and the support server. Owner controls are the hidden prefix group `!candyadmin` (not a slash command): `status`, `power on|off`, `test_session [minutes] [interval] [#channel]`, `test_session_stop`, and `wrapup [dry_run]`.

| Field | Type | Description |
|-------|------|-------------|
| `streak_factor` | float | Multiplier applied per consecutive claimed drop. `0 < x <= 1`. Default: `0.6`. |
| `streak_floor_low_minutes` | int | Shortest gap the streak may shrink the minimum interval to before the late ramp. Never raises the configured minimum. Default: `12`. |
| `streak_floor_high_minutes` | int | Shortest gap the streak may shrink the maximum interval to before the late ramp. Never raises the configured maximum. Default: `15`. |
| `late_floor_minutes` | int | Floor for both ends of the interval from `late_ramp_start` through the end of the window. `1`–`60`. Never raises the configured minimum or maximum. Default: `1`. |
| `late_ramp_start` | string | Local `HH:MM` on the event date when the speedup starts and the late floor takes over. `00:00`–`23:59`. Default: `18:00`. |
| `late_ramp_full` | string | Local `HH:MM` when the speedup reaches `late_ramp_factor`. May run past midnight (`24:00`–`48:00`; `30:00` is 6:00 AM the next day) but not past `window_end`. Must be after the start. Default: `21:00`. |
| `late_ramp_factor` | float | Gap multiplier from `late_ramp_full` through the end of the window, including after midnight. `0 < x <= 1`. Default: `0.5`. |
| `window_start` | string | Local `HH:MM` on `event_date` when drops may start. `00:00`–`23:59`. Default: `10:00`. |
| `window_end` | string | When drops stop, as `HH:MM` after local midnight of `event_date`. `24:00` is the next midnight. `30:00` is 6:00 AM the next morning. Up to `48:00`. Must be after `window_start`. Default: `30:00`. |

The default hunt runs from Oct 31 10:00 AM through Nov 1 6:00 AM Pacific. From 6:00 PM the gaps ease toward half, and from 9:00 PM they stay at half until 6:00 AM. After 6:00 PM a claim streak can bring both ends of the gap down to 1 minute. Each scheduled real drop stays up for `min(drop_expiry, max(2 minutes, twice the current high end of the gap))`. A drop posted just before 6:00 AM can still be claimed after the window ends. Test-session drops keep their own lifetime. The wrap-up post stays at Nov 1 10:00 AM Pacific and counts candy claimed through that morning. If `wrap_up_at` is earlier than the window end, the bot logs a warning and keeps going.

The same object also accepts the other schedule keys the cog already reads (`event_date`, `min_interval_minutes`, `max_interval_minutes`, `drop_expiry_minutes`, `trick_chance`, and the announcement and wrap-up fields).

## Palworld operational config (separate file)

Server process settings (auto-restart, backups, log rotation) are in **`palworld_config.json`** at the repo root, not in `server_configs/`.

Copy `palworld_config.example.json` → `palworld_config.json`.

RCON/Steam credentials remain in `cogs/utils/palworld_utils/palworld.json` (copy from `palworld.example.json`; never commit).

## Adding a new guild

1. Copy `server_configs/example.json` to `server_configs/<guild_id>.json`.
2. Set `enabled: true` and fill in channel/role IDs.
3. Restart the bot (or `!reload_cog` for cogs that read config at runtime — most cache on load).
4. Run `!sync_commands` if you added slash commands scoped to that guild.

## Runtime data files

Per-guild prompt lists and global trackers are stored under `data/`:

- `data/gametime.json`
- `data/birthday.json`
- `data/bingo.json`
- `data/game_picker.json`

Legacy files in the repo root are moved into `data/` automatically on bot startup.

## Bot status

Run `!status` in Discord for uptime, Lavalink/Wavelink, ComfyUI queue, Palworld, and configured guild count.