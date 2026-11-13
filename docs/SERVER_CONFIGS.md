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

Optional overrides for the Halloween candy hunt (`cogs/candyhunt.py`). A missing section keeps the cog defaults. Invalid values are ignored. Running it again next year (calendar, channels, odds, announcements, and resetting `data/candy_hunt.json`) is documented in `docs/README_candyhunt.md`.

`halloween_enabled` is a bot-wide boolean in `config.json`, not a key in the guild file (see `config.example.json`). Missing means `false`: the candy hunt cog, the costume cog, and `/halloweenify` stay unloaded. Set it to `true` and restart to bring them back.

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

### `turkey_hunt`

Optional overrides for the Thanksgiving turkey hunt (`cogs/turkeyhunt.py`, planned by `cogs/hunt/`). A missing section keeps the cog defaults. Invalid values are ignored. `guild_id` is not overridable. The full operator note is `docs/README_turkeyhunt.md`.

`thanksgiving_enabled` is a bot-wide boolean in `config.json`, not a key in the guild file (see `config.example.json`). Missing means `false`. The turkey cog still loads, so `!turkeyadmin` can preview and run test drops. The scheduled live event does nothing until the flag is `true` and the bot is restarted: no kickoff, drops, recaps, winner post, cleanup, or avatar change. This does not turn Halloween on. `cogs.turkeyhunt` is not a Halloween extension.

Drops are posted only in the channel id `drop_channel_id`. The default is `932057681307512924`, MTS #general, the same id as `MAIN_DROP_CHANNEL_ID` in `cogs/candyhunt.py`. The bot looks that channel up by id. It does not search by name.

The public slash command is `/turkeys`, registered for Menaces to Sobriety and the support server the same way as `/candy`. Loading this cog does not sync commands. After a later approved restart, Nick runs `!sync_commands ~` himself in each of those guilds. Owner controls are the hidden prefix group `!turkeyadmin` (not a slash command): `status`, `schedule [save]`, `cleanup [true|false]` (omitted means dry-run), `testdrop <channel>`, `test_session [minutes] [interval] [channel]`, `test_session_stop`, `simulate [iso]`, and `power on|off`. Test sessions and test drops are refused in Menaces to Sobriety and on the live drop channel. Cleanup runs only in Menaces to Sobriety.

`scripts/turkeyhunt_ctl.py` is the local file inbox for the same test actions (`testdrop`, `test_session start|stop`, `status`, `schedule`). It does not connect to Discord. The default channel is `1045149015756521493`, `SUPPORT_TEST_CHANNEL_ID` in `cogs/candyhunt.py`. `schedule` from that path is a preview and does not save the live plan. Details are in `docs/README_turkeyhunt.md`.

| Field | Type | Description |
|-------|------|-------------|
| `timezone` | string | IANA zone for every clock. Default: `America/Los_Angeles`. |
| `drop_channel_id` | int | The only channel that receives live drops. Default: `932057681307512924` (MTS #general). |
| `event_start` | string | ISO datetime for the kickoff and the first moment drops may post. Default: Friday 2026-11-20 05:00 Pacific. |
| `drops_end` | string | ISO datetime when open turkeys close and no further drops post. Default: Sunday 2026-11-29 22:00 Pacific. |
| `final_at` | string | ISO datetime for the winner post and cleanup. Default: Monday 2026-11-30 10:00 Pacific. |
| `window_start` / `window_end` | string | Local `HH:MM` drop window. Defaults: `05:00` and `22:00`. |
| `early_start` / `early_end` | string | Every day gets a drop in this range. Defaults: `05:00` and `08:00`. |
| `late_start` / `late_end` | string | Every regular day gets a drop in this range. Defaults: `20:00` and `22:00`. |
| `sunday_last_drop` | string | Last drop on the finale date (the local date of `drops_end`, Sunday 2026-11-29). Default: `21:30`. Sunday 2026-11-22 is a regular day and may drop through `22:00`. |
| `recap_time` | string | Local `HH:MM` daily recap, including the finale Sunday. Default: `22:30`. |
| `min_gap_minutes` / `max_gap_minutes` | int | Inclusive gap between drops on the same day. Defaults: `20` and `180`. |
| `cluster_gap_minutes` | int | A cluster is a gap at or under this many minutes. Every day has at least one. Default: `30`. |
| `lifetime_minutes` | int | How long a turkey stays up. Default: `45`. |
| `thanksgiving_lifetime_minutes` | int | Lifetime on `thanksgiving_date`. Default: `30`. |
| `regular_low` / `regular_high` / `regular_weights` | int / int / number[] | Count range and bias for days that are not Thanksgiving and not the finale. Defaults: `6`, `10`, `[1, 2, 4, 6, 5]`. |
| `thanksgiving_date` | string | `YYYY-MM-DD`. Default: `2026-11-26`. |
| `thanksgiving_low` / `thanksgiving_high` / `thanksgiving_weights` | int / int / number[] | Defaults: `18`, `22`, `[1, 2, 5, 2, 1]`. |
| `finale_low` / `finale_high` / `finale_weights` | int / int / number[] | The `drops_end` date. Defaults: `8`, `10`, `[2, 5, 3]`. |
| `target_total` | int | The planner nudges the biased draw onto this total when it sits inside the raw low/high bound. Default: `100`. The raw bound for these ranges is 74–112. |
| `regular_weight` / `plump_weight` / `golden_weight` | float | Appearance odds, `0`–`1`. Defaults: `0.75`, `0.20`, `0.05`. |
| `regular_points` / `plump_points` / `golden_points` | int | Defaults: `10`, `30`, `100`. |
| `grease_chance` | float | Slip chance on Regular turkeys only. Default: `0.1`. |
| `prize_line` | string | Shown on the winner post. Empty means `Prize: (not set yet)`. Role pings are rejected. |
| `avatar_path` | string | Pilgrim avatar, relative to the repo. Default: `assets/avatars/pilgrim_shepherd.png`. A missing file skips the avatar change and still posts the kickoff. |
| `guarantee_key` | string | Forced when the last Thanksgiving drop is rolled and that type has not appeared yet. Default: `golden`. |
| `cleanup_skip_channel_ids` | int[] | Never delete in these channels. Default includes MTS announcements `1019352642977267743`. |

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