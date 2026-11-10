# Thanksgiving turkey hunt

The 2026 Menaces to Sobriety turkey hunt is a second theme on the hunt engine in `cogs/hunt/`. Halloween still runs through `cogs/candyhunt.py`. This file does not change that hunt.

Nothing in this implementation starts the live event. Drops, the kickoff, recaps, the winner post, cleanup, and the avatar stay off until Nick sets `thanksgiving_enabled` to `true` in `config.json` and restarts the bot later.

## Switches

`thanksgiving_enabled` lives in `config.json` (see `config.example.json`). A missing key is `false`.

`cogs.turkeyhunt` is always loaded. It is not in the Halloween extension set, so `halloween_enabled` does not load or unload it. While `thanksgiving_enabled` is false, the scheduled live event does nothing. `!turkeyadmin` still works for status, a schedule preview, simulation, and test drops outside Menaces to Sobriety.

`!turkeyadmin power` is a second, local pause stored in `data/turkey_hunt.json`. Turning that local pause off does not override `thanksgiving_enabled: false`.

## Slash command

`/turkeys` shows your score and the leaderboard. It is guild-scoped to Menaces to Sobriety and the support server, same as `/candy`. The cog does not sync commands. After a later approved restart, Nick runs `!sync_commands ~` himself in each of those servers. This pass does not do that.

## Channel

Live drops go only to MTS #general, channel id `932057681307512924`. That id is `MAIN_DROP_CHANNEL_ID` in `cogs/candyhunt.py` (`# MTS #general`). The bot looks the channel up by that id. It does not search by name. The id is the `drop_channel_id` default and is also set in `server_configs/932057681307512922.json`.

## Calendar

Times are `America/Los_Angeles`.

- Friday 2026-11-20 05:00: kickoff, then drops start. The kickoff lists Regular, Plump, and Golden odds and points, the 45-minute lifetime (30 minutes on Thanksgiving), the greased-feather slip on regulars, and the end times.
- Drops land at random times from 05:00 to 22:00, with gaps from 20 minutes to 3 hours. A cluster is a gap of 30 minutes or less. Every day has at least one cluster, one drop from 05:00–08:00, and one from 20:00–22:00.
- Regular days, including Sunday 2026-11-22, plan 6–10 turkeys. Thanksgiving 2026-11-26 plans 18–22, and those live 30 minutes. Sunday 2026-11-29 plans 8–10, and the last one is by 21:30.
- The raw ranges add up to 74–112. The planner nudges a biased draw onto 100, which is inside that bound.
- About 1 in 10 grabs of a Regular turkey slip. The turkey stays up. The score does not move. Plump and Golden do not grease.
- Odds when a turkey appears: Regular 75% / 1 point, Plump 20% / 3 points, Golden 5% / 10 points. If Thanksgiving Day has not rolled a Golden yet, the last Nov 26 drop is Golden. Missed slots are not replaced.
- Daily recap at 22:30 in #general, including Sunday Nov 29 (after the 22:00 close).
- Sunday 2026-11-29 22:00: open turkeys are closed or deleted. Nothing can be caught after that instant.
- Monday 2026-11-30 10:00: winner post with the top 10, fun stats, and an explicit tie line when places are shared, including a tie for first. The prize line is empty by default (`Prize: (not set yet)`). Nick can set `turkey_hunt.prize_line` later.
- That same Monday post deletes leftover turkey drops and the daily recaps. It keeps the winner post and the kickoff. It deletes only this bot's own hunt messages, matched by stored id, hunt marker, and author. Announcement channels are skipped. User messages are not deleted. Then it writes `data/turkey_hunt_archive_2026.json`, turns drops off, and resets the live book.

State is `data/turkey_hunt.json` (gitignored with the rest of `data/*.json`). A restart does not invent drops. A slot that is already posted is not posted again. A slot whose lifetime has ended is marked missed. A slot that is late but still inside its original lifetime is posted with that original expiry.

## Avatar

At kickoff, if this event has not already tried, the bot snapshots its current global avatar and sets `assets/avatars/pilgrim_shepherd.png`. At the Monday final it restores that snapshot. Each direction runs at most once. If the pilgrim file is missing, the bot logs a warning, skips the avatar, and still posts the kickoff. It does not retry later in the same event. No avatar call is made while `thanksgiving_enabled` is false.

## Admin

`!turkeyadmin` is a hidden, owner-only prefix group. Anyone else gets no reply.

- `status`
- `schedule` previews the saved plan, or an unsaved preview. `schedule save` writes the plan without posting.
- `cleanup` is a dry run unless the argument is `false`. Menaces to Sobriety only.
- `testdrop <channel>` and `test_session` post outside Menaces to Sobriety and outside the live drop channel. Scores go to the test tally.
- `simulate` previews a clock time and does not save.
- `power on|off` is the local pause.

Test commands keep working while the live switch is off.
