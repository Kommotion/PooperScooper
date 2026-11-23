# Changelog

This is the first versioned changelog for PooperScooper, covering about Oct 14–Nov 15, 2026.

## v2.8.4 — 2026-11-23

### New stuff

- Older image gens now show the model they were made with.
- The all-time image-gen panel tracks since May 2025.
- The all-time image-gen panel lists how many gens used each model.
- !play and /play, and other prefix and slash twins, are one combined row.
- Weekly busiest hours only show this week's hours.
- Least-used commands only list commands the bot currently has loaded.
- The weekly stats card now posts in #illuminaughty.

### Background

- Caption model labels from the older hyphen form are counted. Preview text alone is not.
- The image-gen date comes from the earliest recorded image gen. Voice still uses the live-tracking date.
- Prefix and slash counts for the same command are summed on the weekly and all-time lists.

## v2.8.3 — 2026-11-23

### New stuff

- After Sunday's post, !statscard and previews show the week that just closed instead of an empty new-week stub under the old title.
- The weekly model mix no longer borrows all-time numbers. A week with no image gens says n/a.
- Weekly top commands only lists commands that were actually used that week.
- Owner-only admin commands stay off the command lists.
- All-time and weekly snapshots can include historical #image-diffusion gens and historical MTS slash commands.

### Background

- The close saves the full closed-week card, and a rebuild script can refill the Nov 16 week.
- Owner-only names come from a prefix-versus-slash filter.
- History backfill scripts are idempotent, REST-only, and MTS only.

## v2.8.2 — 2026-11-22

### New stuff

- Inbox and Party Prompt image jobs now count on the stats cards like /imagegen.
- All-time quiet channels show real names and only rooms that have had messages.
- Least-used commands now shows the bottom of the real command list, including unused commands.
- All-time history will go back to the start of the MTS server (January 2022).

### Background

- Image stats hook runs on the background generation path using the job guild id.
- Idempotent closed-week imagegen correction helper.
- Command split is top 5 and least-used 5.
- Card name lookup covers all-time channel lists.

## v2.8.1 — 2026-11-21

### New stuff

- Sunday's stats post is now two cards, This Week and All Time, so week numbers and history are not mixed in one table. The week card has vs-last-week changes; all-time is since May 18.
- Custom emojis on the cards now show the real image plus the name.
- All-time history goes back about six months. Image gen and voice say when tracking started, instead of looking like a long stretch of zeros.
- Command rankings only count real bot commands (no glued leftovers like !playhttps://...).

### Background

- Offline REST backfill script (checkpoints, no double-counting, does not take the live bot down)
- On-disk emoji image cache
- Command registry filter and a one-time cleanup of junk prefix-command rows
- Tests updated for the two cards, backfill, emoji cache, and command filter

## v2.8.0 — 2026-11-18

### New stuff

- A weekly stats card posts every Sunday at 9 PM PT. It will move to #illuminaughty later.
- The card is a dashboard of busiest and quietest channels, dead channels, top reactions, least-used custom emojis, commands, image generation, active members versus last week, busiest hours in PT, and voice.
- `/stats optout` stops counting you as an active member in this server. `/stats optin` turns that back on.

### Background

- New weekly stats cog stores only aggregate counts in SQLite, plus this week's active-member list (cleared weekly) and the opt-out list. No message text, prompts, or per-person totals.
- Owner command `!statscard` posts the current card without closing the week.

## v2.7.3 — 2026-11-16

### Background

- Image buttons no longer error when a deleted message's timer runs out, or when a click arrives after the interaction expired.

## v2.7.2 — 2026-11-15

### New stuff

- Inbox / Party Prompt image jobs can now post in the MTS image channel, in the same style as /imagegen, credited to MTS Party Prompts. Mentions stay off.

### Background

- Removed the hard block on the production MTS image channel. Jobs still post only to the configured channel, and a job that names a different channel is refused.

## v2.7.1 — 2026-11-15

### New stuff

- Inbox generate jobs now post with the same caption, prompt details, and buttons as /imagegen generate, attributed to MTS Party Prompts.

## v2.7.0 — 2026-11-15

### New stuff

- The bot can pick up image jobs dropped in an inbox folder and post them to a configured channel. This stays off unless `mts_image_jobs.enabled` is turned on. The default channel is the support test channel, not MTS #image-diffusion.

## v2.6.1 — 2026-11-15

### New stuff

- Image edits with huge photos no longer time out

## v2.6.0 — 2026-11-14

### New stuff

- new image models Qwen-Image 2.1, FLUX 3 Klein (realism), LUSTIFY (realism, NSFW)
- /imagegen models tagged [Anime][NSFW]/[Realism][SFW]
- upgraded One Obsession Anima v5, One Obsession Illustrious v24, better Identity Edit
- image queue now 30 (was 5)
- removed Z-Image Turbo (defaults move to WAI Illustrious)
- new /grok and /remind
- Malfoyd 5 shared daily rerolls and attachment fallback for broken previews
- playlists keep playing when a song errors
- hidden easter eggs
- Thanksgiving turkey hunt engine (coming soon, off by default)

### Background

- ComfyUI engine upgrade to 0.45.3
- music/Lavalink reliability
- voice status
- Shepherd's Farm server config
