"""Shared JSON-backed per-guild prompt list used by Bingo and GamePicker."""

from __future__ import annotations

import logging
from difflib import SequenceMatcher

import discord
from discord import app_commands

from cogs.utils.json_store import LockedJsonFile

log = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.85
MAX_EMBED_CHARS = 4096
MAX_AUTOCOMPLETE_CHOICES = 25


class GuildPromptList:
    def __init__(self, json_path: str, *, indent: int | None = 4):
        self._store = LockedJsonFile(json_path, default=dict, indent=indent)
        self.data: dict[str, list[str]] = {}

    async def load(self) -> None:
        self.data = await self._store.aread()

    async def dump(self) -> None:
        await self._store.awrite(self.data)

    def get_list(self, guild_id: int) -> list[str]:
        return list(self.data.get(str(guild_id), []))

    def _prompt_already_exists(self, existing_lowercase: list[str], new_prompt: str) -> tuple[bool, str | None]:
        for existing_prompt in existing_lowercase:
            if SequenceMatcher(None, existing_prompt, new_prompt).ratio() >= SIMILARITY_THRESHOLD:
                return True, existing_prompt
        return False, None

    async def add(self, guild_id: int, prompt: str) -> tuple[bool, str]:
        guild_key = str(guild_id)
        prompt = prompt.strip()
        if not prompt:
            return False, "Prompt cannot be empty."

        lowercase_prompt = prompt.lower()
        try:
            existing_lowercase = [entry.lower() for entry in self.data[guild_key]]
            already_exists, matching_prompt = self._prompt_already_exists(existing_lowercase, lowercase_prompt)
            if already_exists:
                return False, f"A matching prompt already exists:\n{matching_prompt}"
            self.data[guild_key].append(prompt)
        except KeyError:
            self.data[guild_key] = [prompt]
        except Exception as e:
            log.exception("Unable to add prompt: %s", e)
            return False, "An unknown error occurred."

        await self.dump()
        return True, ""

    async def remove(self, guild_id: int, prompt: str) -> bool:
        guild_key = str(guild_id)
        prompt = prompt.strip()
        lowercase_prompt = prompt.lower()

        try:
            existing_lowercase = [entry.lower() for entry in self.data[guild_key]]
            prompt_index = existing_lowercase.index(lowercase_prompt)
            del self.data[guild_key][prompt_index]
        except KeyError:
            return False
        except ValueError:
            return False

        await self.dump()
        return True

    def autocomplete_choices(self, guild_id: int, current: str) -> list[app_commands.Choice[str]]:
        items = self.get_list(guild_id)
        if not items:
            return []
        choices = [
            app_commands.Choice(name=item, value=item)
            for item in items
            if current.lower() in item.lower()
        ]
        return choices[:MAX_AUTOCOMPLETE_CHOICES]


def build_list_embeds(title: str, items: list[str], color: discord.Color = discord.Color.blue()) -> list[discord.Embed]:
    embeds: list[discord.Embed] = []
    current_description = ""

    for item in sorted(items, key=str.lower):
        new_description = f"{current_description}{item}\n"
        if len(new_description) > MAX_EMBED_CHARS:
            embeds.append(discord.Embed(title=title, description=current_description, color=color))
            current_description = f"{item}\n"
        else:
            current_description = new_description

    if current_description:
        embeds.append(discord.Embed(title=title, description=current_description, color=color))

    return embeds