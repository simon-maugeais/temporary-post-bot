from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from config import DurationOption

if TYPE_CHECKING:
    from services.posts import PostService

logger = logging.getLogger(__name__)
CUSTOM_ID_PREFIX = "temporary-post:lifetime:"


class LifetimeButton(discord.ui.Button):
    def __init__(self, option: DurationOption, service: PostService) -> None:
        super().__init__(
            label=option.label,
            style=discord.ButtonStyle.primary,
            custom_id=f"{CUSTOM_ID_PREFIX}{option.seconds}",
        )
        self.seconds = option.seconds
        self.service = service

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.service.select_lifetime(interaction, self.seconds)


class LifetimeView(discord.ui.View):
    def __init__(self, service: PostService, options: tuple[DurationOption, ...]) -> None:
        super().__init__(timeout=None)
        for option in options:
            self.add_item(LifetimeButton(option, service))

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        logger.error(
            "Lifetime interaction failed in channel %s", interaction.channel_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        content = "Something went wrong. Please try again; any saved expiration remains scheduled."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True)
            else:
                await interaction.response.send_message(content, ephemeral=True)
        except discord.HTTPException:
            logger.exception("Could not report interaction error in channel %s", interaction.channel_id)
