# Running the candy hunt next year

Next year: set halloween_enabled to true in config.json and restart.

The hunt is Menaces to Sobriety. The defaults in `cogs/candyhunt.py` are the 2026 event. Override them in `server_configs/932057681307512922.json` under `candy_hunt`. A missing key keeps the default. A bad value is logged and ignored. `guild_id` is not overridable.

Stop the bot before you edit the state file. Do not edit `data/candy_hunt.json` while the bot is running.

## Calendar

| Key | Default | What it does |
| --- | --- | --- |
| `event_date` | `2026-10-31` | `YYYY-MM-DD` of the hunt morning. |
| `window_start` | `10:00` | Local clock on `event_date` when drops may start. `00:00`–`23:59`. |
| `window_end` | `30:00` | Drops stop. `30:00` is 6:00 AM the next day. Up to `48:00`. Must be after the start. |
| `wrap_up_at` | `2026-11-01T10:00:00` | When the winner post goes out. A naive datetime uses `timezone`. |
| `lights_out` | window end | When the "hunt is over" post goes out. Omit it, or set `null`, to use `window_end`. |
| `timezone` | `America/Los_Angeles` | IANA name. `timezone_name` is the same key. |
| `late_ramp_start` | `18:00` | Gap speedup, the switch from day treats/tricks to the night tables, and the night-shift announcement. |
| `late_ramp_full` | `21:00` | When the gap reaches `late_ramp_factor`. May pass midnight (`30:00` is 6:00 AM) but not `window_end`. |
| `multi_drop_start` | `18:00` | First minute a batch can land in more than one channel. |
| `multi_drop_late` | `19:00` | Hotter multi-drop odds start. |
| `frenzy_start` | `19:30` | Witching Hour announcement, and the first minute a cleared batch can start a frenzy. |

## Channels and announcements

| Key | Default | What it does |
| --- | --- | --- |
| `announcement_channel_id` | `1019352642977267743` | `#announcements`. Cleanup never deletes here. |
| `announcement_channel_name` | `announcements` | Used when the id is not in the guild. |
| `main_drop_channel_id` | `932057681307512924` | `#general`. Always the first drop in a multi-drop batch. |
| `multi_drop_channel_names` | `gaming`, `sharing-is-caring`, `food-porn`, `music` | Extra rooms, matched by channel name. |
| `night_shift_enabled` | `true` | 6 PM night-shift post. Set `false` to skip it. |
| `witching_hour_enabled` | `true` | 7:30 Witching Hour post. Set `false` to skip it. |
| `announcement_grace_minutes` | `30` | Do not post a scheduled note if the bot wakes up more than this many minutes late. A note also stays quiet once `window_end` has passed, so a restart late on hunt night does not repeat it. |

The night-shift and Witching Hour posts each fire once. The done flags are `night_shift_done` and `witching_hour_done` in `data/candy_hunt.json`. A missing flag counts as not done. The post is saved as done before Discord sends it.

## Optional tuning

These keys also live under `candy_hunt`. Leave them out to keep this year's behavior.

- `trick_chance`, `night_trick_chance`
- `treat_weights`, `night_treat_weights` (candy name to weight, or a list in treat order)
- `trick_weights`, `night_trick_weights` (a list in trick order; two tricks share the same loss, so this is not keyed by amount)
- `multi_drop_odds`, `multi_drop_odds_late` (list of `[count, weight]`)
- `frenzy_chance`, `frenzy_rounds`, `frenzy_drop_count`, `frenzy_gap_seconds`, `frenzy_kick_delay`
- `leader_trick_chance`, `leader_big_loss_multiplier`, `leader_big_loss_amounts`
- Interval, expiry, streak, and Pumpkin God keys already read by the cog (`min_interval_minutes`, `max_interval_minutes`, `drop_expiry_minutes`, `streak_factor`, `late_ramp_factor`, `late_floor_minutes`, `pumpkin_god_days`, and the rest in `docs/SERVER_CONFIGS.md`)

## Reset the book

Stop the bot. Copy `data/candy_hunt.json` to something like `data/candy_hunt_2026.json`. Delete `data/candy_hunt.json`. The next launch creates a fresh book. That clears totals and the done flags (`night_shift_done`, `witching_hour_done`, `lights_out_done`, `wrap_up_done`).

## Admin commands

Owner only, in Discord, prefix group `!candyadmin` (not a slash command):

- `!candyadmin wrapup` or `!candyadmin wrapup true` — preview the winner post. Nothing is sent.
- `!candyadmin wrapup false` — post the real wrap-up once, in Menaces to Sobriety. This is irreversible.
- `!candyadmin cleanup true` — count this bot's candy-hunt messages in the drop channels.
- `!candyadmin cleanup false` — delete those messages. Announcements are skipped. Old curse and refund lines still count as hunt messages.
- `!candyadmin status` — show the window, ramp, and next drop.
