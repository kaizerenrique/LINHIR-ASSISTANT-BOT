"""Linhir Assistant Bot — Entry point."""

import logging
import sys
import traceback

import discord
from discord import app_commands
from discord.ext import commands

from api_client import APIClient
from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

COGS = (
    "cogs.utilities",
    "cogs.identity",
)


class LinhirAssistant(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        for ext in COGS:
            try:
                await self.load_extension(ext)
                logger.info("✅ Cog cargado: %s", ext)
            except Exception as exc:
                logger.error("❌ Fallo cargando %s: %s", ext, exc, exc_info=exc)

        try:
            if Config.DEV_GUILD_ID:
                guild = discord.Object(id=Config.DEV_GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info("🔄 Sync DEV guild %s: %d comandos", Config.DEV_GUILD_ID, len(synced))
            else:
                synced = await self.tree.sync()
                logger.info("🔄 Sync GLOBAL: %d comandos", len(synced))
        except Exception as exc:
            logger.error("❌ Fallo sincronizando comandos: %s", exc, exc_info=exc)

    async def on_ready(self):
        logger.info("✅ Conectado como %s (ID: %s)", self.user, self.user.id)
        logger.info("📡 Servidores: %d", len(self.guilds))
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{Config.GUILD_NAME} · /hora · /oro",
            )
        )

    async def close(self):
        logger.info("🛑 Cerrando bot...")
        await APIClient.close()
        await super().close()


bot = LinhirAssistant()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    logger.error("Slash command error: %s\n%s", error, traceback.format_exc())
    msg = "❌ Ocurrió un error inesperado. Intenta de nuevo en un momento."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        Config.validate()
    except ValueError as exc:
        logger.critical("Config inválida: %s", exc)
        sys.exit(1)

    try:
        bot.run(Config.DISCORD_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        logger.info("👋 Detenido manualmente.")
    except Exception as exc:
        logger.critical("Error fatal: %s", exc, exc_info=exc)
        sys.exit(1)