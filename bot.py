import os
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("music-bot")

# We only need default intents + voice state tracking (for auto-leave logic).
# message_content is NOT enabled because everything is done via slash commands.
intents = discord.Intents.default()
intents.voice_states = True


class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Load the music cog (this is where all /commands live)
        await self.load_extension("cogs.music")

        # Push slash commands to Discord.
        synced = await self.tree.sync()
        log.info(f"Synced {len(synced)} slash command(s).")


bot = MusicBot()


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log.info("Bot is ready.")


def main():
    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN not found. Copy .env.example to .env and add your bot token."
        )
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
